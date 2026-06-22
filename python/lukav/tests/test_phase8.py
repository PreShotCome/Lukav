"""Phase 8 tests: credit-report bulk ingest, CFPB lookup, debt-buyer match."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from lukav.cfpb_lookup import CfpbResult, lookup as cfpb_lookup
from lukav.collections_engine import get_collection, list_collections
from lukav.dispute_engine import save_profile
from lukav.ingest_credit_report import (
    Tradeline, extract_credit_report, ingest_credit_report,
    to_collection_payload,
)
from lukav.legal import debt_buyers
from lukav.llm.base import ChatMessage, LlmClient
from lukav.tests.fakes import FakePlaid
from lukav.web.app import create_app


class FakeLLM(LlmClient):
    def __init__(self, payload):
        import json as _json
        self.content = payload if isinstance(payload, str) else _json.dumps(payload)

    def chat(self, messages, tools=None, temperature=0.2):
        return ChatMessage(role="assistant", content=self.content)


SAMPLE_REPORT_TEXT = """\
EQUIFAX CREDIT REPORT

NEGATIVE ACCOUNTS
-----------------

MIDLAND CREDIT MGMT (Original creditor: Capital One)
Account #: ****4242
Balance: $1,234.56
Date opened: 2018-06-01
Date of first delinquency: 2018-12-15
Last activity: 2019-03-01
Status: Collection account

PORTFOLIO RECOVERY ASSOC (Original creditor: Synchrony Bank)
Account #: ****9911
Balance: $487.10
Date opened: 2019-01-15
Status: Charge-off
"""


# ---- LLM extraction -----------------------------------------------------

def test_extract_credit_report_parses_array():
    fake = FakeLLM([
        {
            "collector_name": "Midland Credit Management",
            "original_creditor": "Capital One",
            "alleged_amount": 1234.56,
            "account_mask": "4242",
            "date_opened": "2018-06-01",
            "date_of_first_delinquency": "2018-12-15",
            "last_activity_date": "2019-03-01",
            "status": "in_collection",
            "bureau": "Equifax",
            "notes": "",
        },
        {
            "collector_name": "Portfolio Recovery Associates",
            "original_creditor": "Synchrony Bank",
            "alleged_amount": 487.10,
            "account_mask": "9911",
            "date_opened": "2019-01-15",
            "date_of_first_delinquency": None,
            "last_activity_date": None,
            "status": "charged_off",
            "bureau": "Equifax",
            "notes": "",
        },
    ])
    result = extract_credit_report(SAMPLE_REPORT_TEXT, llm_client=fake)
    assert len(result.tradelines) == 2
    assert result.tradelines[0].collector_name == "Midland Credit Management"
    assert result.tradelines[0].alleged_amount == 1234.56
    assert result.tradelines[0].account_mask == "4242"
    assert result.tradelines[1].status == "charged_off"


def test_extract_credit_report_handles_garbage_json():
    fake = FakeLLM("sorry, I cannot extract")
    result = extract_credit_report(SAMPLE_REPORT_TEXT, llm_client=fake)
    assert result.tradelines == []
    assert "JSON" in result.error or "did not" in result.error or "array" in result.error


def test_extract_credit_report_with_fences():
    fake = FakeLLM(
        '```json\n[{"collector_name": "X", "alleged_amount": 0, '
        '"original_creditor": "", "account_mask": "", "date_opened": null, '
        '"date_of_first_delinquency": null, "last_activity_date": null, '
        '"status": "in_collection", "bureau": "", "notes": ""}]\n```'
    )
    result = extract_credit_report(SAMPLE_REPORT_TEXT, llm_client=fake)
    assert len(result.tradelines) == 1
    assert result.tradelines[0].collector_name == "X"


def test_status_outside_enum_coerced_to_default():
    fake = FakeLLM([{
        "collector_name": "Y", "original_creditor": "", "alleged_amount": 0,
        "account_mask": "", "date_opened": None,
        "date_of_first_delinquency": None, "last_activity_date": None,
        "status": "weird-status-from-llm",
        "bureau": "", "notes": "",
    }])
    result = extract_credit_report("text", llm_client=fake)
    assert result.tradelines[0].status == "in_collection"


# ---- to_collection_payload ----------------------------------------------

def test_payload_uses_dofd_when_last_activity_missing():
    t = Tradeline(
        collector_name="X", date_of_first_delinquency="2019-01-01",
        last_activity_date=None,
    )
    payload = to_collection_payload(t, fallback_state="TX")
    assert payload["last_activity_date"] == "2019-01-01"
    assert payload["state"] == "TX"
    assert "DOFD 2019-01-01" in payload["notes"]


# ---- CFPB lookup --------------------------------------------------------

def test_cfpb_lookup_empty_company_returns_empty_result():
    result = cfpb_lookup("")
    assert result.total_hits == 0
    assert result.recent == []
    assert result.error is None


def test_cfpb_lookup_parses_api_response():
    fake_payload = {
        "hits": {
            "total": {"value": 42},
            "hits": [
                {"_source": {
                    "company": "Midland Credit Management",
                    "issue": "False statements or representation",
                    "sub_issue": "Attempted to collect wrong amount",
                    "state": "TX",
                    "date_received": "2024-03-15T00:00:00",
                    "complaint_what_happened": "They called me at 6am threatening suit.",
                }},
            ],
        },
        "aggregations": {
            "issue": {"issue": {"buckets": [
                {"key": "False statements", "doc_count": 12},
                {"key": "Communication tactics", "doc_count": 7},
            ]}},
        },
    }

    class _R:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _R(fake_payload)

    with patch("lukav.cfpb_lookup.httpx.Client", _Client):
        result = cfpb_lookup("Midland Credit Management")

    assert result.total_hits == 42
    assert len(result.recent) == 1
    assert result.recent[0].issue == "False statements or representation"
    assert result.recent[0].date_received == "2024-03-15"
    assert len(result.top_issues) == 2
    assert result.top_issues[0] == ("False statements", 12)


def test_cfpb_lookup_handles_http_error():
    class _ErrClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): raise RuntimeError("boom")

    with patch("lukav.cfpb_lookup.httpx.Client", _ErrClient):
        result = cfpb_lookup("X")
    assert result.error is not None
    assert "boom" in result.error


# ---- Debt buyer match ---------------------------------------------------

def test_debt_buyer_matches_midland():
    p = debt_buyers.match("Midland Credit Management, Inc.")
    assert p is not None
    assert p.name == "Midland Credit Management"


def test_debt_buyer_matches_alias():
    p = debt_buyers.match("Encore Capital Recovery")
    assert p is not None
    assert "Encore" in p.name or "Midland" in p.name


def test_debt_buyer_no_match():
    assert debt_buyers.match("My Local Hospital Billing") is None


# ---- Routes -------------------------------------------------------------

def test_credit_report_form_renders():
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/credit-report")
    assert resp.status_code == 200
    assert "Credit-report ingest" in resp.text


def test_credit_report_extract_and_save(monkeypatch):
    fake = FakeLLM([
        {"collector_name": "Midland Credit Management",
         "original_creditor": "Capital One", "alleged_amount": 1234.56,
         "account_mask": "4242", "date_opened": "2018-06-01",
         "date_of_first_delinquency": "2018-12-15",
         "last_activity_date": "2019-03-01",
         "status": "in_collection", "bureau": "Equifax", "notes": ""},
        {"collector_name": "Portfolio Recovery Associates",
         "original_creditor": "Synchrony Bank", "alleged_amount": 487.10,
         "account_mask": "9911", "date_opened": "2019-01-15",
         "date_of_first_delinquency": None, "last_activity_date": None,
         "status": "charged_off", "bureau": "Equifax", "notes": ""},
    ])
    monkeypatch.setattr(
        "lukav.ingest_credit_report.build_default_client",
        lambda *a, **kw: fake,
    )

    app = create_app(plaid=FakePlaid())
    client = TestClient(app)

    # Extract.
    resp = client.post(
        "/credit-report",
        files={"file": ("report.txt", SAMPLE_REPORT_TEXT.encode(), "text/plain")},
        data={"state": "TX"},
    )
    assert resp.status_code == 200
    assert "Midland Credit Management" in resp.text
    assert "Portfolio Recovery Associates" in resp.text
    assert "1234.56" in resp.text

    # Save — keep first row only.
    resp = client.post("/credit-report/save", data={
        "state": "TX",
        "row_count": "2",
        "keep_0": "1",
        "collector_name_0": "Midland Credit Management",
        "original_creditor_0": "Capital One",
        "alleged_amount_0": "1234.56",
        "account_mask_0": "4242",
        "date_of_first_delinquency_0": "2018-12-15",
        "last_activity_date_0": "2019-03-01",
        "status_0": "in_collection",
        "bureau_0": "Equifax",
        "notes_0": "",
        # row 1 not ticked
    }, follow_redirects=False)
    assert resp.status_code == 303
    accts = list_collections()
    assert len(accts) == 1
    assert accts[0].collector_name == "Midland Credit Management"
    assert accts[0].state == "TX"


def test_collection_detail_shows_debt_buyer_badge(monkeypatch):
    from lukav.collections_engine import add_collection
    coll_id = add_collection({
        "collector_name": "Midland Credit Management",
        "collector_address": "",
        "original_creditor": "Capital One",
        "alleged_amount": 100.0,
        "status": "in_collection",
        "state": "TX",
    })
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get(f"/collections/{coll_id}")
    assert resp.status_code == 200
    assert "Debt buyer detected" in resp.text
    assert "Midland Credit Management" in resp.text


def test_collection_cfpb_route_renders(monkeypatch):
    from lukav.collections_engine import add_collection
    coll_id = add_collection({
        "collector_name": "Test Collector",
        "alleged_amount": 0, "status": "in_collection", "state": "TX",
    })

    fake_payload = {
        "hits": {"total": {"value": 0}, "hits": []},
        "aggregations": {"issue": {"issue": {"buckets": []}}},
    }

    class _R:
        def raise_for_status(self): pass
        def json(self): return fake_payload

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _R()

    monkeypatch.setattr("lukav.cfpb_lookup.httpx.Client", _Client)
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get(f"/collections/{coll_id}/cfpb")
    assert resp.status_code == 200
    assert "CFPB complaints" in resp.text
    assert "Test Collector" in resp.text
