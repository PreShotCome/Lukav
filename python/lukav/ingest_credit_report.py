"""Credit-report PDF ingest — pull every negative tradeline out of an
Equifax / Experian / TransUnion PDF and turn each into a
CollectionAccount.

Pipeline mirrors ingest.py:
  1. pypdf extracts text
  2. Claude returns a strict JSON ARRAY of tradeline dicts
  3. UI shows the table; user confirms which rows to import
  4. POST /credit-report/save creates one CollectionAccount per chosen row

The LLM is asked to skip current/good-standing accounts — Lukav's job is
collections and credit repair, not the whole report."""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from lukav.ingest import _parse_json_response
from lukav.llm import ChatMessage, build_default_client


SYSTEM_PROMPT = (
    "You are a strict JSON extractor. You will be given a CHUNK of "
    "text from a consumer credit report. Extract EVERY tradeline that "
    "appears in this chunk, including:\n"
    "  - active collections, charge-offs, accounts in dispute, accounts "
    "sold to debt buyers\n"
    "  - any account with a late payment in its history (30 / 60 / 90 / "
    "120 / 150 / 180-day late), even if it's currently \"open\" or "
    "\"paid\"\n"
    "  - closed accounts with any negative history\n"
    "  - bankruptcies, repossessions, foreclosures, deficiencies, liens, "
    "judgments\n\n"
    "Only SKIP an account when it is shown as both (a) currently open AND "
    "(b) never-late / pays-as-agreed with NO late marks at all in its "
    "payment history. When in doubt, INCLUDE it — the user can untick.\n\n"
    "HARD RULES — read carefully:\n"
    "  - Use ONLY values that appear verbatim in the input. If a field "
    "is not present in the text, use empty string / 0 / null.\n"
    "  - Do NOT infer amounts from balance ranges. Do NOT guess dates. "
    "Do NOT invent collector or creditor names from common-sounding "
    "ones.\n"
    "  - For DATE fields: parse them in any format the credit bureau "
    "uses (MM/DD/YYYY, YYYY-MM-DD, 'Jun 2018', 'June 15, 2018', etc.) "
    "and emit them as ISO YYYY-MM-DD. If only month+year is shown, "
    "use the FIRST of the month. If you cannot find the field in this "
    "chunk, set it to null — a regex pass will try to fill it later.\n"
    "  - Set status to the most fitting value: in_collection | "
    "charged_off | sold | disputed | settled | paid | removed. If the "
    "tradeline is currently open with late history, use \"in_collection\" "
    "only when actually in collections; otherwise use the bureau's "
    "status if it fits — otherwise \"disputed\". Status \"paid\" means "
    "paid in full; we still extract these when they had lates.\n"
    "  - If this chunk contains NO tradelines, return [].\n\n"
    "Respond with one compact JSON array — no prose before or after, "
    "no markdown fences. Each element MUST match this shape:\n\n"
    "[\n"
    "  {\n"
    '    "collector_name": "",\n'
    '    "original_creditor": "",\n'
    '    "alleged_amount": 0,\n'
    '    "account_mask": "",\n'
    '    "date_opened": null,\n'
    '    "date_of_first_delinquency": null,\n'
    '    "last_activity_date": null,\n'
    '    "status": "in_collection",\n'
    '    "bureau": "",\n'
    '    "notes": ""\n'
    "  }\n"
    "]\n"
)


CHUNK_SIZE = 12000          # larger so a single tradeline stays whole
CHUNK_OVERLAP = 1500        # generous overlap so boundary tradelines hit twice
TRADELINE_HEAD_RE = re.compile(
    r"^[ \t]*(?:[A-Z][A-Z0-9 ,&./'\-]{4,60})\s*$",
    re.MULTILINE,
)

# Words that mark a chunk as worth sending to the LLM. Credit reports are
# dominated by good-standing accounts, personal info, inquiries, and
# disclaimers — sending those to the LLM is just paying for noise. We
# only send chunks that hit at least one of these keywords.
NEGATIVE_KEYWORDS = (
    "collection", "collections", "charge-off", "charge off", "chargeoff",
    "charged off", "past due", "delinquent", "delinquency",
    "deficiency", "repossession", "bankruptcy",
    "settled", "settlement", "settled for less",
    "120 days", "150 days", "180 days", "90 days",
    "60 days", "30 days late", "in dispute",
    "negative account", "negative item", "negative info",
    "transferred / sold", "transferred/sold", "sold to",
    "judgement", "judgment", "lien",
    "midland", "portfolio recovery", "lvnv", "cavalry",
    "jefferson capital", "encore", "resurgent", "convergent",
)


def _chunk_text(text: str) -> list[str]:
    """Split the report into overlapping ~12k-char chunks. Prefers
    breaking at a tradeline-header line (a line of mostly capital
    letters that looks like a creditor name), then at blank lines, then
    hard-cuts. Larger chunks + bigger overlap mean a single tradeline
    almost never gets split across two LLM calls."""
    text = text.strip()
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(i + CHUNK_SIZE, len(text))
        if end < len(text):
            window = text[end - 2000:end]
            # Prefer tradeline header (a creditor-name line) within the
            # trailing 2k chars.
            head_matches = list(TRADELINE_HEAD_RE.finditer(window))
            if head_matches:
                end = end - 2000 + head_matches[-1].start()
            else:
                break_at = window.rfind("\n\n")
                if break_at >= 0:
                    end = end - 2000 + break_at
        chunks.append(text[i:end])
        if end >= len(text):
            break
        i = max(end - CHUNK_OVERLAP, i + 1)
    return chunks


def _chunk_is_relevant(chunk: str) -> bool:
    """Cheap pre-filter: only ask the LLM about chunks that contain
    negative-account keywords. Cuts a 20-chunk Experian report down to
    typically 2-4 LLM calls."""
    lower = chunk.lower()
    return any(kw in lower for kw in NEGATIVE_KEYWORDS)


# ---- regex field pre-extraction ----------------------------------------

# Date patterns we'll accept verbatim from credit reports. Bureaus print
# dates in a half-dozen common forms — keep them all.
_DATE_RE = (
    r"(\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r")"
)

# Labels for each field, as seen in Experian / Equifax / TransUnion.
_LABEL_PATTERNS: dict[str, list[re.Pattern]] = {
    "date_opened": [
        re.compile(rf"Date\s+Opened[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Opened[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Account\s+Opened[:\s]+{_DATE_RE}", re.IGNORECASE),
    ],
    "date_of_first_delinquency": [
        re.compile(rf"Date\s+of\s+First\s+Delinquency[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"First\s+Delinquency[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"\bDOFD[:\s]+{_DATE_RE}", re.IGNORECASE),
    ],
    "last_activity_date": [
        re.compile(rf"Last\s+Activity[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Date\s+of\s+Last\s+Activity[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Date\s+of\s+Last\s+Payment[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Last\s+Payment[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Last\s+Reported[:\s]+{_DATE_RE}", re.IGNORECASE),
        re.compile(rf"Date\s+Reported[:\s]+{_DATE_RE}", re.IGNORECASE),
    ],
}


def _normalize_date(raw: str) -> Optional[str]:
    """Coerce a date string from any of the bureau formats into
    YYYY-MM-DD, or None if it can't be parsed."""
    if not raw:
        return None
    raw = raw.strip().strip(",")
    fmts = (
        "%Y-%m-%d",
        "%m/%d/%Y", "%m/%d/%y",
        "%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y",
        "%b %Y", "%B %Y",
    )
    from datetime import datetime
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _find_block_for_collector(text: str, collector_name: str,
                              window_chars: int = 2500) -> str:
    """Return the chunk of source text starting at the collector name —
    used to scope a regex search to one tradeline. We only look FORWARD
    (with a small 60-char back-buffer for line context), because credit
    reports lay out fields below the creditor name; looking back would
    bleed into the previous tradeline's date fields."""
    if not collector_name:
        return ""
    tokens = [t for t in re.split(r"\W+", collector_name) if len(t) >= 4]
    if not tokens:
        return ""
    lower = text.lower()
    needle = tokens[0].lower()
    idx = lower.find(needle)
    if idx < 0:
        return ""
    start = max(0, idx - 60)
    end = min(len(text), idx + window_chars)
    return text[start:end]


def _regex_fill_dates(tradeline: "Tradeline", text: str) -> "Tradeline":
    """For any date field the LLM left blank, search the surrounding
    source block by label pattern. Updates `tradeline` in place."""
    block = _find_block_for_collector(text, tradeline.collector_name)
    if not block:
        return tradeline
    for field in ("date_opened", "date_of_first_delinquency",
                  "last_activity_date"):
        if getattr(tradeline, field):
            continue
        for pat in _LABEL_PATTERNS[field]:
            m = pat.search(block)
            if m:
                normalized = _normalize_date(m.group(1))
                if normalized:
                    setattr(tradeline, field, normalized)
                    break
    return tradeline


@dataclass
class Tradeline:
    collector_name: str = ""
    original_creditor: str = ""
    alleged_amount: float = 0.0
    account_mask: Optional[str] = None
    date_opened: Optional[str] = None
    date_of_first_delinquency: Optional[str] = None
    last_activity_date: Optional[str] = None
    status: str = "in_collection"
    bureau: str = ""
    notes: str = ""


@dataclass
class CreditReportExtraction:
    tradelines: list[Tradeline] = field(default_factory=list)
    raw_text: str = ""
    backend: str = "none"
    error: str = ""
    chunks_total: int = 0
    chunks_processed: int = 0
    chunks_with_keywords: int = 0
    raw_extracted_count: int = 0      # before grounding / dedup
    ungrounded_dropped: int = 0
    duplicates_dropped: int = 0


def extract_text_from_pdf(data: bytes) -> str:
    """Try pdfplumber first (table-aware), fall back to pypdf. Returns
    empty string if both fail."""
    text = _extract_with_pdfplumber(data)
    if text:
        return text
    return _extract_with_pypdf(data)


def _extract_with_pdfplumber(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            parts: list[str] = []
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                parts.append(t)
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _extract_with_pypdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception:
        return ""


def _coerce_tradeline(d: dict) -> Tradeline:
    try:
        amount = float(d.get("alleged_amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    status = (d.get("status") or "in_collection").lower().strip()
    if status not in {"in_collection", "charged_off", "sold", "disputed",
                      "settled", "paid", "removed", "open_with_lates",
                      "closed_with_lates"}:
        status = "in_collection"
    return Tradeline(
        collector_name=str(d.get("collector_name") or "").strip(),
        original_creditor=str(d.get("original_creditor") or "").strip(),
        alleged_amount=amount,
        account_mask=(str(d.get("account_mask") or "").strip() or None),
        date_opened=_norm_date(d.get("date_opened")),
        date_of_first_delinquency=_norm_date(d.get("date_of_first_delinquency")),
        last_activity_date=_norm_date(d.get("last_activity_date")),
        status=status,
        bureau=str(d.get("bureau") or "").strip(),
        notes=str(d.get("notes") or "").strip(),
    )


def _norm_date(value) -> Optional[str]:
    if not value:
        return None
    s = str(value)[:10]
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


def extract_credit_report(text: str, *,
                          llm_client=None) -> CreditReportExtraction:
    out = CreditReportExtraction(raw_text=text)
    if not text.strip():
        return out
    client = llm_client if llm_client is not None else build_default_client()
    if client is None:
        out.backend = "none"
        out.error = ("No LLM available. Set LUKAV_LLM_BACKEND=claude with "
                     "the `claude` CLI on PATH, or fill the tradelines by "
                     "hand using /collections/new.")
        return out
    out.backend = client.__class__.__name__

    chunks = _chunk_text(text)
    out.chunks_total = len(chunks)
    out.chunks_with_keywords = sum(1 for c in chunks if _chunk_is_relevant(c))

    raw_lower = text.lower()
    seen_keys: set[tuple[str, str]] = set()
    errors: list[str] = []

    # Process every chunk. The previous "skip chunks without negative
    # keywords" gate dropped too many real tradelines on bureaus that
    # interleave payment-history grids without using words like
    # "collection" or "charge-off". Trust the SYSTEM_PROMPT's
    # "SKIP current accounts" instruction instead.
    for idx, chunk in enumerate(chunks):
        try:
            msg = client.chat(
                messages=[
                    ChatMessage(role="system", content=SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=f"[chunk {idx + 1} of {len(chunks)}]\n\n{chunk}",
                    ),
                ],
                temperature=0.0,
            )
            out.chunks_processed += 1
        except Exception as e:
            errors.append(f"chunk {idx + 1}: {e}")
            continue

        data = _parse_first_array(msg.content)
        if data is None:
            errors.append(
                f"chunk {idx + 1}: LLM did not return a JSON array."
            )
            continue

        for d in data:
            if not isinstance(d, dict):
                continue
            tradeline = _coerce_tradeline(d)
            out.raw_extracted_count += 1
            if not _is_grounded(tradeline, text, raw_lower):
                out.ungrounded_dropped += 1
                continue
            _regex_fill_dates(tradeline, text)
            # Dedup key includes original_creditor + rounded amount so
            # the same collector with multiple different debts doesn't
            # collapse to one row when account masks are missing.
            key = (
                tradeline.collector_name.lower().strip(),
                (tradeline.account_mask or "").strip(),
                tradeline.original_creditor.lower().strip(),
                round(tradeline.alleged_amount, 0),
            )
            if key in seen_keys:
                out.duplicates_dropped += 1
                continue
            seen_keys.add(key)
            out.tradelines.append(tradeline)

    for t in out.tradelines:
        _regex_fill_dates(t, text)

    if errors:
        out.error = " | ".join(errors[:3])
    return out


def _is_grounded(tradeline: "Tradeline", text: str, raw_lower: str) -> bool:
    """A tradeline is grounded when:
      - collector_name has at least one significant token (>=4 chars),
        AND a sufficient fraction of those tokens appear verbatim in the
        source text (case-insensitive). Tolerates abbreviation ("MGMT"
        vs "Management") because the rest of the name usually matches.
      - if alleged_amount is nonzero, the amount appears in the text
        as either "1234.56" or "1234".
    """
    if not tradeline.collector_name:
        return False
    tokens = [t.lower() for t in re.split(r"\W+", tradeline.collector_name)
              if len(t) >= 4]
    if not tokens:
        return False
    matches = sum(1 for t in tokens if t in raw_lower)
    required = 1 if len(tokens) == 1 else 2
    if matches < required:
        return False
    if tradeline.alleged_amount > 0:
        # Credit reports print amounts as "$1,234.56" — strip commas before
        # matching so the comma'd form grounds fine.
        text_nocommas = text.replace(",", "")
        amount_str = f"{tradeline.alleged_amount:.2f}"
        if (amount_str not in text_nocommas
                and f"{int(tradeline.alleged_amount)}" not in text_nocommas):
            return False
    return True


def _parse_first_array(text: str) -> Optional[list]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
        if m:
            raw = m.group(1)
    start = raw.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def ingest_credit_report(data: bytes, filename: str,
                         *, llm_client=None) -> CreditReportExtraction:
    text = extract_text_from_pdf(data) if filename.lower().endswith(".pdf") \
        else data.decode("utf-8", errors="replace")
    return extract_credit_report(text, llm_client=llm_client)


def to_collection_payload(t: Tradeline, *, fallback_state: str = "") -> dict:
    """Map an extracted tradeline into add_collection()'s payload."""
    last_activity = t.last_activity_date or t.date_of_first_delinquency
    notes_parts = []
    if t.bureau:
        notes_parts.append(f"Bureau: {t.bureau}.")
    if t.date_opened:
        notes_parts.append(f"Opened {t.date_opened}.")
    if t.date_of_first_delinquency:
        notes_parts.append(f"DOFD {t.date_of_first_delinquency}.")
    if t.notes:
        notes_parts.append(t.notes)
    return {
        "collector_name": t.collector_name,
        "collector_address": "",
        "original_creditor": t.original_creditor,
        "alleged_amount": t.alleged_amount,
        "status": t.status,
        "first_contact_date": None,
        "last_activity_date": last_activity,
        "state": fallback_state,
        "account_mask": t.account_mask,
        "notes": " ".join(notes_parts),
    }
