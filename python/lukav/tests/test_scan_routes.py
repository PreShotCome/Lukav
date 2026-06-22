"""Web-route tests for the scan UI."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from lukav.tests.fakes import FakePlaid, make_sample_dataset
from lukav.web.app import create_app


def test_scan_get_404_for_unknown_account(monkeypatch):
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/scan/does-not-exist")
    assert resp.status_code == 404


def test_full_scan_flow(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    accts, liabs, txns = make_sample_dataset()
    # Force a discrepancy: spike balance over limit.
    accts[0].current_balance = 6000.0
    accts[0].credit_limit = 5000.0
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)
    app = create_app(plaid=fake)
    client = TestClient(app)
    client.post("/exchange", data={"public_token": "p1"})

    # Save context.
    resp = client.post(
        f"/scan/{accts[0].account_id}/context",
        data={
            "state": "TX",
            "last_activity_date": (date.today() - timedelta(days=365 * 10)).isoformat(),
            "collection_letter_received": "1",
            "collection_letter_date": date.today().isoformat(),
            "credit_report_dispute_basis": "Reported as open when closed",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Run scan.
    resp = client.post(f"/scan/{accts[0].account_id}", follow_redirects=False)
    assert resp.status_code == 303

    # GET shows findings.
    resp = client.get(f"/scan/{accts[0].account_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Balance exceeds the disclosed credit limit" in body
    assert "Debt may be past the statute of limitations" in body
    assert "Right to demand debt validation" in body
    # Every finding shows a citation (we never surface uncited claims).
    assert "Citation:" in body
