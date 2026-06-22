"""Web route tests using FakePlaid — no network, no real Plaid creds."""
from __future__ import annotations

from fastapi.testclient import TestClient

from lukav.storage import db
from lukav.tests.fakes import FakePlaid, make_sample_dataset
from lukav.web.app import create_app


def test_index_renders_without_cards():
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No cards linked yet" in resp.text


def test_link_without_creds_shows_help(monkeypatch):
    # No PLAID_CLIENT_ID / PLAID_SECRET set (autouse fixture strips them).
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/link")
    assert resp.status_code == 200
    assert "Plaid credentials are not configured" in resp.text


def test_link_with_creds_renders_token(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    fake = FakePlaid()
    app = create_app(plaid=fake)
    client = TestClient(app)
    resp = client.get("/link")
    assert resp.status_code == 200
    assert "Plaid.create" in resp.text
    assert "link-sandbox-ian-0" in resp.text


def test_exchange_persists_item_and_syncs_data(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    accts, liabs, txns = make_sample_dataset()
    fake = FakePlaid(
        institution_name="Big Bank",
        accounts=accts, liabilities=liabs, transactions=txns,
    )
    app = create_app(plaid=fake)
    client = TestClient(app)

    resp = client.post("/exchange", data={"public_token": "public-sandbox-xyz"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["item_id"] == "item-public-sandbox-xyz"
    assert body["synced"]["accounts"] == 1
    assert body["synced"]["transactions"] == 5

    # Item + account + liability are in the DB.
    items = db.list_items()
    assert len(items) == 1
    assert items[0].institution_name == "Big Bank"
    accounts = db.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].mask == "4242"
    liab = db.get_liability(accounts[0].account_id)
    assert liab and liab.minimum_payment_amount == 75.00


def test_dashboard_lists_card_after_exchange(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    accts, liabs, txns = make_sample_dataset()
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)
    app = create_app(plaid=fake)
    client = TestClient(app)
    client.post("/exchange", data={"public_token": "p1"})

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Visa Signature" in resp.text
    assert "24.99%" in resp.text
    assert "$2300.00" in resp.text   # statement balance


def test_account_page_lists_transactions(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    accts, liabs, txns = make_sample_dataset()
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)
    app = create_app(plaid=fake)
    client = TestClient(app)
    client.post("/exchange", data={"public_token": "p1"})

    resp = client.get(f"/account/{accts[0].account_id}")
    assert resp.status_code == 200
    assert "Visa Signature" in resp.text
    assert "Local Cafe" in resp.text


def test_sync_all_idempotent(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    accts, liabs, txns = make_sample_dataset()
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)
    app = create_app(plaid=fake)
    client = TestClient(app)
    client.post("/exchange", data={"public_token": "p1"})

    resp = client.post("/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["items"] == 1
    assert body["accounts"] == 1
