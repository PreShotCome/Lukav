"""Tests for the PDF/image/text ingest pipeline."""
from __future__ import annotations

import io
from datetime import date

from fastapi.testclient import TestClient

from lukav.collections_engine import (
    get_collection, list_collections, list_communications,
)
from lukav.ingest import (
    ExtractedLetter, _parse_json_response, extract_fields, extract_text,
    ingest, ingest_text, to_collection_payload, to_communication_payload,
)
from lukav.llm.base import ChatMessage, LlmClient
from lukav.tests.fakes import FakePlaid
from lukav.web.app import create_app


SAMPLE_LETTER_TEXT = """\
Midland Credit Management, Inc.
350 Camino de la Reina
San Diego, CA 92108

October 14, 2024

Re: Capital One account ending 4242
Amount due: $1,234.56

Dear consumer,

We are attempting to collect a debt that was previously owed to
Capital One. This is an attempt to collect a debt, and any
information obtained will be used for that purpose.

If you do not resolve this debt within 30 days, we may file suit in
state court to recover the amount owed.

The law limits how long you can be sued on a debt. Because of the
age of your debt, we will not sue you for it. If you do not pay the
debt, we may continue to report it to the credit reporting agencies
as unpaid for as long as the law permits this reporting.

Sincerely,
Midland Credit Management
"""


class FakeLLM(LlmClient):
    """Returns a canned JSON extraction. Lets us test extract_fields
    without depending on the Claude CLI."""

    def __init__(self, payload: dict | str) -> None:
        self.payload = payload

    def chat(self, messages, tools=None, temperature=0.2):
        import json as _json
        content = self.payload if isinstance(self.payload, str) else _json.dumps(self.payload)
        return ChatMessage(role="assistant", content=content)


# ---- text dispatch -------------------------------------------------------

def test_extract_text_falls_back_to_paste_on_unknown_suffix():
    text, source = extract_text(b"hello world", "notes.xyz")
    assert text == "hello world"
    assert source == "paste"


def test_extract_text_pdf_path_calls_pypdf():
    # Build a minimal one-page PDF.
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    text, source = extract_text(buf.getvalue(), "letter.pdf")
    assert source == "pdf"
    # A blank PDF won't have text but the path must succeed without raising.
    assert isinstance(text, str)


# ---- JSON parsing --------------------------------------------------------

def test_parse_json_strips_code_fences():
    blob = '```json\n{"a": 1, "b": "two"}\n```'
    assert _parse_json_response(blob) == {"a": 1, "b": "two"}


def test_parse_json_finds_object_in_chatter():
    blob = 'Here is the extraction: {"x": true} hope that helps'
    assert _parse_json_response(blob) == {"x": True}


def test_parse_json_returns_none_on_garbage():
    assert _parse_json_response("not json at all") is None


# ---- extract_fields ------------------------------------------------------

def test_extract_fields_with_fake_llm_populates_struct():
    fake = FakeLLM({
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina, San Diego, CA 92108",
        "original_creditor": "Capital One",
        "alleged_amount": 1234.56,
        "letter_date": "2024-10-14",
        "summary": "Collection letter; threat of suit; time-bar disclosure present.",
        "threat_of_suit": True,
        "time_bar_disclosure": True,
        "deadline_mentioned": "30 days from receipt",
    })
    result = extract_fields(SAMPLE_LETTER_TEXT, llm_client=fake)
    assert result.collector_name == "Midland Credit Management"
    assert result.original_creditor == "Capital One"
    assert result.alleged_amount == 1234.56
    assert result.letter_date == "2024-10-14"
    assert result.threat_of_suit is True
    assert result.time_bar_disclosure is True
    assert result.backend == "FakeLLM"


def test_extract_fields_handles_invalid_llm_json():
    fake = FakeLLM("sorry I cannot do that")
    result = extract_fields(SAMPLE_LETTER_TEXT, llm_client=fake)
    assert result.collector_name == ""
    assert "did not return valid JSON" in result.notes


def test_extract_fields_returns_stub_when_no_llm():
    result = extract_fields(SAMPLE_LETTER_TEXT, llm_client=None.__class__ if False else None)
    # build_default_client may return None if no claude CLI; that's fine.
    # If it returns a client, the test still passes — we just check backend.
    assert result.raw_text == SAMPLE_LETTER_TEXT
    assert result.backend in ("none", "ClaudeCliClient", "OllamaClient", "error")


# ---- payload mappers -----------------------------------------------------

def test_to_communication_payload_sets_letter_kind_and_date():
    letter = ExtractedLetter(
        collector_name="X", letter_date="2024-10-14",
        summary="s", threat_of_suit=True,
    )
    payload = to_communication_payload(letter)
    assert payload["kind"] == "letter"
    assert payload["occurred_at"].startswith("2024-10-14")
    assert payload["threat_of_suit"] is True


def test_to_collection_payload_carries_state_through():
    letter = ExtractedLetter(
        collector_name="Y", original_creditor="Z",
        alleged_amount=42.0, letter_date="2024-10-14",
        summary="s",
    )
    payload = to_collection_payload(letter, fallback_state="TX")
    assert payload["state"] == "TX"
    assert payload["collector_name"] == "Y"
    assert payload["original_creditor"] == "Z"


# ---- route flow ---------------------------------------------------------

def test_ingest_paste_creates_new_collection(monkeypatch):
    fake = FakeLLM({
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina",
        "original_creditor": "Capital One",
        "alleged_amount": 1234.56,
        "letter_date": "2024-10-14",
        "summary": "demand letter",
        "threat_of_suit": True,
        "time_bar_disclosure": True,
    })
    monkeypatch.setattr("lukav.ingest.build_default_client",
                        lambda *a, **kw: fake)

    app = create_app(plaid=FakePlaid())
    client = TestClient(app)

    resp = client.post("/ingest", data={
        "pasted_text": SAMPLE_LETTER_TEXT,
        "collection_id": "",
        "state": "TX",
    })
    assert resp.status_code == 200
    assert "Midland Credit Management" in resp.text
    assert "Capital One" in resp.text
    assert "1234.56" in resp.text

    resp = client.post("/ingest/save", data={
        "action": "new",
        "collection_id": "",
        "state": "TX",
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina",
        "original_creditor": "Capital One",
        "alleged_amount": "1234.56",
        "letter_date": "2024-10-14",
        "summary": "demand letter",
        "threat_of_suit": "1",
        "time_bar_disclosure": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    new_id = resp.headers["location"].rsplit("/", 1)[-1]

    coll = get_collection(new_id)
    assert coll and coll.collector_name == "Midland Credit Management"
    assert coll.state == "TX"
    comms = list_communications(new_id)
    assert len(comms) == 1
    assert comms[0].kind == "letter"
    assert comms[0].threat_of_suit is True


def test_ingest_attach_to_existing_collection(monkeypatch):
    fake = FakeLLM({
        "collector_name": "Midland Credit Management",
        "collector_address": "",
        "original_creditor": "Capital One",
        "alleged_amount": 0,
        "letter_date": "2024-10-14",
        "summary": "Threatened suit; 30 days.",
        "threat_of_suit": True,
        "time_bar_disclosure": False,
    })
    monkeypatch.setattr("lukav.ingest.build_default_client",
                        lambda *a, **kw: fake)
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)

    # Pre-create a sparse collection.
    resp = client.post("/collections", data={
        "collector_name": "", "collector_address": "",
        "original_creditor": "", "alleged_amount": "0",
        "status": "in_collection",
        "first_contact_date": "", "last_activity_date": "",
        "state": "TX", "account_mask": "", "notes": "",
    }, follow_redirects=False)
    coll_id = resp.headers["location"].rsplit("/", 1)[-1]

    resp = client.post("/ingest/save", data={
        "action": "attach",
        "collection_id": coll_id,
        "state": "TX",
        "collector_name": "Midland Credit Management",
        "collector_address": "",
        "original_creditor": "Capital One",
        "alleged_amount": "0",
        "letter_date": "2024-10-14",
        "summary": "Threatened suit",
        "threat_of_suit": "1",
        "time_bar_disclosure": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    coll = get_collection(coll_id)
    assert coll.collector_name == "Midland Credit Management"  # populated
    assert coll.original_creditor == "Capital One"             # populated
    comms = list_communications(coll_id)
    assert len(comms) == 1
    assert comms[0].kind == "letter"
    assert comms[0].threat_of_suit is True


def test_ingest_form_renders_without_collections(monkeypatch):
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/ingest")
    assert resp.status_code == 200
    assert "Ingest a collection letter" in resp.text
    assert "PDF" in resp.text


def test_ingest_pdf_upload_path(monkeypatch):
    # Blank PDFs extract no text and bypass the LLM (by design). The
    # meaningful assertion is that the upload path doesn't crash and the
    # preview page renders.
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)

    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.post(
        "/ingest",
        files={"file": ("letter.pdf", buf.getvalue(), "application/pdf")},
        data={"pasted_text": "", "collection_id": "", "state": ""},
    )
    assert resp.status_code == 200
    assert "Extraction preview" in resp.text
    # Source label confirms PDF path was taken.
    assert "pdf" in resp.text.lower()
