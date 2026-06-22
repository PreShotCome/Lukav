"""Letter ingest — turn a PDF, image, or pasted text into a structured
view (collector, original creditor, amount, date, threats) the user can
confirm and save as a Communication.

Extraction pipeline:
  1. text from input — pypdf for PDFs, pytesseract for images (optional),
     pasted text passes through.
  2. structured fields from text — Claude is asked for strict JSON via
     the existing llm/ client. If no LLM is available, return a stub
     with the raw text in a `notes` field for the user to fill manually.

No ingest path raises on a missing dep — the UI degrades gracefully."""
from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from lukav.llm import ChatMessage, build_default_client


@dataclass
class ExtractedLetter:
    """Structured view of one ingested letter."""
    raw_text: str = ""
    source: str = "paste"            # 'pdf' | 'image' | 'paste'
    backend: str = "none"            # 'claude' | 'ollama' | 'none' | 'error'
    collector_name: str = ""
    collector_address: str = ""
    original_creditor: str = ""
    alleged_amount: float = 0.0
    letter_date: Optional[str] = None    # YYYY-MM-DD
    summary: str = ""
    threat_of_suit: bool = False
    time_bar_disclosure: bool = False
    deadline_mentioned: Optional[str] = None
    notes: str = ""


# ---- text extraction ----------------------------------------------------

def extract_text_from_pdf(data: bytes) -> str:
    """Best-effort PDF text extraction. Returns empty string on failure."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


def extract_text_from_image(data: bytes) -> str:
    """OCR via pytesseract if available. Returns empty string when the
    binary or library is missing — the UI then asks the user to paste
    the text manually."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        return (pytesseract.image_to_string(img) or "").strip()
    except Exception:
        return ""


def extract_text(data: bytes, filename: str) -> tuple[str, str]:
    """Dispatch by extension. Returns (text, source-label)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(data), "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}:
        return extract_text_from_image(data), "image"
    # Unknown — try as UTF-8 text.
    try:
        return data.decode("utf-8", errors="replace").strip(), "paste"
    except Exception:
        return "", "paste"


# ---- LLM structured extraction ------------------------------------------

SYSTEM_PROMPT = (
    "You are a strict JSON extractor. You will be given the text of a "
    "letter from a debt collector. Extract the fields below and respond "
    "with ONE compact JSON object — no prose before or after, no markdown "
    "fences, no commentary. If a field is unclear, use empty string for "
    "strings, 0 for numbers, false for booleans, null for letter_date.\n\n"
    "{\n"
    '  "collector_name": "",\n'
    '  "collector_address": "",\n'
    '  "original_creditor": "",\n'
    '  "alleged_amount": 0,\n'
    '  "letter_date": null,                     // YYYY-MM-DD or null\n'
    '  "summary": "",                            // one short paragraph\n'
    '  "threat_of_suit": false,\n'
    '  "time_bar_disclosure": false,             // CFPB time-barred notice present\n'
    '  "deadline_mentioned": null                // e.g. "30 days from receipt"\n'
    "}\n"
)


def _parse_json_response(text: str) -> Optional[dict]:
    """Pull the first JSON object out of the response — tolerant of code
    fences or surrounding chatter."""
    text = text.strip()
    if not text:
        return None
    # Strip fences if present.
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if match:
            text = match.group(1)
    # Find first balanced {...}.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def extract_fields(text: str, *, llm_client=None) -> ExtractedLetter:
    """Run the LLM extraction. `llm_client=None` builds the default
    (Claude). Tests inject a fake."""
    result = ExtractedLetter(raw_text=text, source="paste",
                             notes=(text or "")[:2000])
    if not text.strip():
        return result

    client = llm_client if llm_client is not None else build_default_client()
    if client is None:
        result.backend = "none"
        return result

    try:
        msg = client.chat(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=text[:12000]),
            ],
            temperature=0.0,
        )
        result.backend = client.__class__.__name__
        data = _parse_json_response(msg.content)
        if not isinstance(data, dict):
            result.notes = (
                f"LLM did not return valid JSON. Raw response:\n"
                f"{msg.content[:1500]}"
            )
            return result
    except Exception as e:
        result.backend = "error"
        result.notes = f"LLM call failed: {e}"
        return result

    # Map fields, guarding types.
    result.collector_name = str(data.get("collector_name") or "")
    result.collector_address = str(data.get("collector_address") or "")
    result.original_creditor = str(data.get("original_creditor") or "")
    try:
        result.alleged_amount = float(data.get("alleged_amount") or 0)
    except (TypeError, ValueError):
        result.alleged_amount = 0.0
    ld = data.get("letter_date")
    if isinstance(ld, str) and len(ld) >= 10:
        try:
            date.fromisoformat(ld[:10])
            result.letter_date = ld[:10]
        except ValueError:
            result.letter_date = None
    result.summary = str(data.get("summary") or "")
    result.threat_of_suit = bool(data.get("threat_of_suit"))
    result.time_bar_disclosure = bool(data.get("time_bar_disclosure"))
    dm = data.get("deadline_mentioned")
    result.deadline_mentioned = str(dm) if dm else None
    return result


# ---- ingest pipeline ----------------------------------------------------

def ingest(data: bytes, filename: str, *, llm_client=None) -> ExtractedLetter:
    text, source = extract_text(data, filename)
    result = extract_fields(text, llm_client=llm_client)
    result.source = source
    return result


def ingest_text(text: str, *, llm_client=None) -> ExtractedLetter:
    result = extract_fields(text, llm_client=llm_client)
    result.source = "paste"
    return result


# ---- helpers for the web layer ------------------------------------------

def to_communication_payload(letter: ExtractedLetter) -> dict:
    """Map an ExtractedLetter into add_communication()'s payload shape."""
    occurred = letter.letter_date or date.today().isoformat()
    return {
        "kind": "letter",
        "occurred_at": occurred + "T00:00",
        "summary": letter.summary or letter.notes[:500],
        "threat_of_suit": letter.threat_of_suit,
        # PDFs of collection letters never give us third-party / workplace
        # / abuse signals — those come from phone-call logs.
    }


def to_collection_payload(letter: ExtractedLetter, *,
                          fallback_state: str = "") -> dict:
    """Map an ExtractedLetter into add_collection()'s payload shape."""
    return {
        "collector_name": letter.collector_name,
        "collector_address": letter.collector_address,
        "original_creditor": letter.original_creditor,
        "alleged_amount": letter.alleged_amount,
        "status": "in_collection",
        "first_contact_date": letter.letter_date,
        "last_activity_date": None,
        "state": fallback_state,
        "account_mask": None,
        "notes": (
            f"Ingested from {letter.source}. "
            f"{letter.summary or '(no summary)'}"
        ),
    }
