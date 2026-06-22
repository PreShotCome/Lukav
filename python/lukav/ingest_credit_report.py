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
    "You are a strict JSON extractor. You will be given the text of a "
    "consumer credit report (from Equifax, Experian, or TransUnion). "
    "Extract EVERY negative tradeline relevant to debt collection or "
    "credit repair: collection accounts, charge-offs, accounts in "
    "dispute, accounts sold to debt buyers, accounts past due, and "
    "accounts flagged for repossession or bankruptcy. SKIP accounts "
    "shown as current / never late / paid in full / closed in good "
    "standing — those don't help the user.\n\n"
    "Respond with one compact JSON array — no prose before or after, "
    "no markdown fences. Each element MUST match this shape "
    "(use empty string / 0 / null when unsure):\n\n"
    "[\n"
    "  {\n"
    '    "collector_name": "",         // current creditor/collector on this tradeline\n'
    '    "original_creditor": "",      // OC if disclosed; "" otherwise\n'
    '    "alleged_amount": 0,          // balance the tradeline shows\n'
    '    "account_mask": "",           // last 4 of account number if visible\n'
    '    "date_opened": null,          // YYYY-MM-DD or null\n'
    '    "date_of_first_delinquency": null,  // YYYY-MM-DD or null — needed for §1681c math\n'
    '    "last_activity_date": null,   // YYYY-MM-DD or null\n'
    '    "status": "in_collection",     // in_collection | charged_off | sold | disputed | settled | paid | removed\n'
    '    "bureau": "",                  // Equifax | Experian | TransUnion (if identifiable)\n'
    '    "notes": ""                    // anything else that matters: dispute history, comments, etc.\n'
    "  }\n"
    "]\n\n"
    "Return [] if there are no negative tradelines."
)


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
    """Same approach as ingest.extract_text_from_pdf but kept here so
    credit-report ingest stands alone."""
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
    try:
        msg = client.chat(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=text[:60000]),
            ],
            temperature=0.0,
        )
    except Exception as e:
        out.backend = "error"
        out.error = f"LLM call failed: {e}"
        return out

    raw = msg.content.strip()
    # Tolerate fences + leading prose.
    if raw.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
        if m:
            raw = m.group(1)
    # Find first balanced [...].
    start = raw.find("[")
    if start < 0:
        out.error = f"LLM did not return a JSON array. Raw:\n{msg.content[:1500]}"
        return out
    depth = 0
    end = -1
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        out.error = "Unterminated JSON array from LLM."
        return out
    try:
        data = json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        out.error = f"JSON parse failed: {e}"
        return out
    if not isinstance(data, list):
        out.error = "LLM JSON was not a list."
        return out
    out.tradelines = [_coerce_tradeline(d) for d in data if isinstance(d, dict)]
    return out


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
