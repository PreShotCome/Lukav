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
    "text from a consumer credit report. Extract every negative "
    "tradeline that APPEARS IN THIS CHUNK: collection accounts, "
    "charge-offs, accounts in dispute, accounts sold to debt buyers, "
    "accounts past due, and accounts flagged for repossession or "
    "bankruptcy. SKIP accounts shown as current / never late / paid "
    "in full / closed in good standing.\n\n"
    "HARD RULES — read carefully:\n"
    "  - Use ONLY values that appear verbatim in the input. If a field "
    "is not present in the text, use empty string / 0 / null.\n"
    "  - Do NOT infer amounts from balance ranges. Do NOT guess dates. "
    "Do NOT invent collector or creditor names from common-sounding "
    "ones. If you are unsure, return empty.\n"
    "  - If this chunk contains NO negative tradelines, return [].\n\n"
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


CHUNK_SIZE = 6000           # chars per LLM call
CHUNK_OVERLAP = 400         # carry tail into next chunk to catch table-boundary tradelines

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
    """Split the report into overlapping ~6k-char chunks. Prefers
    breaking at blank lines so a single tradeline doesn't get split."""
    text = text.strip()
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(i + CHUNK_SIZE, len(text))
        if end < len(text):
            # Back up to the last blank-line break if there's one within
            # the trailing 800 chars; otherwise hard-cut.
            window = text[end - 800:end]
            break_at = window.rfind("\n\n")
            if break_at >= 0:
                end = end - 800 + break_at
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
                      "settled", "paid", "removed"}:
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
    relevant = [(idx, c) for idx, c in enumerate(chunks)
                if _chunk_is_relevant(c)]
    if not relevant:
        out.error = (
            "No chunks contained negative-account keywords. Either the "
            "report has no collections / charge-offs / late accounts, or "
            "the text extraction missed the negative section. Spot-check "
            "the raw text below."
        )
        return out

    raw_lower = text.lower()
    seen_keys: set[tuple[str, str]] = set()
    errors: list[str] = []
    ungrounded_dropped = 0

    for idx, chunk in relevant:
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
            if not _is_grounded(tradeline, text, raw_lower):
                ungrounded_dropped += 1
                continue
            key = (
                tradeline.collector_name.lower().strip(),
                tradeline.account_mask or "",
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.tradelines.append(tradeline)

    if errors:
        out.error = " | ".join(errors[:3])
    if ungrounded_dropped:
        suffix = f"{ungrounded_dropped} row(s) dropped as ungrounded."
        out.error = (out.error + " " + suffix) if out.error else suffix
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
