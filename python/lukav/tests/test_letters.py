"""Letter generation tests — every CARD-Act finding maps to a template,
every template renders with the user's profile, and the LLM step is
skipped when no backend is configured."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from lukav.audit_engine import scan_account
from lukav.dispute_engine import (
    RULE_TO_TEMPLATE, get_letter, list_letters, render_letter, save_letter,
    save_profile, template_for,
)
from lukav.models.debt_models import Account, Apr, Item, Liability, Transaction
from lukav.storage import db
from lukav.tests.fakes import FakePlaid, make_sample_dataset
from lukav.tools.legal_research import analyze_finding
from lukav.web.app import create_app


def _seed_account_with_findings(monkeypatch):
    db.init_db()
    db.upsert_item(Item(item_id="i1", institution_name="Bank", access_token="x"))
    acct = Account(
        account_id="acct-1", item_id="i1", name="Visa", official_name=None,
        mask="4242", subtype="credit card",
        current_balance=6000.0, available_balance=-1000.0,
        credit_limit=5000.0,
    )
    db.upsert_account(acct)
    db.upsert_liability(Liability(
        account_id="acct-1",
        last_statement_balance=2000.0,
        minimum_payment_amount=75.0,
        aprs=[Apr(apr_percentage=12.0, apr_type="purchase_apr",
                  balance_subject_to_apr=1000.0, interest_charge_amount=20.0)],
    ))
    db.upsert_transaction(Transaction(
        transaction_id="t-late", account_id="acct-1",
        posted_date=date.today() - timedelta(days=5),
        amount=39.0, name="LATE PAYMENT FEE",
    ))
    return acct


def test_every_finding_has_a_template():
    # The known rule_ids (from card_act + fdcpa + fcra YAMLs + billing
    # heuristic) are all mapped to a template.
    expected = {
        "card_act.interest_charge_implied_apr",
        "card_act.first_late_fee_cap",
        "card_act.repeat_late_fee_cap",
        "card_act.over_limit_fee_requires_opt_in",
        "card_act.over_limit_balance",
        "card_act.min_payment_exceeds_balance",
        "billing.possible_duplicate_charge",
        "fdcpa.validation_opportunity",
        "fdcpa.time_barred_debt",
        "fdcpa.cease_contact",
        "fcra.bureau_dispute_opportunity",
        "fcra.direct_furnisher_dispute",
    }
    for rule_id in expected:
        assert rule_id in RULE_TO_TEMPLATE, f"no template for {rule_id}"


def test_render_letter_for_over_limit_finding(monkeypatch):
    acct = _seed_account_with_findings(monkeypatch)
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    findings = scan_account(acct.account_id)
    over_limit = next(f for f in findings
                      if f.rule_id == "card_act.over_limit_balance")
    body = render_letter(over_limit.finding_id)
    assert body is not None
    assert "Ian Test" in body
    assert "Austin, TX 78701" in body
    assert "Re: Billing-error notice" in body
    assert over_limit.citation in body


def test_save_and_list_letter(monkeypatch):
    acct = _seed_account_with_findings(monkeypatch)
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    findings = scan_account(acct.account_id)
    f = findings[0]
    body = render_letter(f.finding_id)
    letter_id = save_letter(f.finding_id, body, template_for(f))
    listed = list_letters()
    assert any(l["letter_id"] == letter_id for l in listed)
    fetched = get_letter(letter_id)
    assert fetched and fetched["body"] == body


def test_analyze_finding_falls_back_when_no_backend(monkeypatch):
    acct = _seed_account_with_findings(monkeypatch)
    findings = scan_account(acct.account_id)
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "none")
    review = analyze_finding(findings[0].finding_id)
    assert review.backend == "none"
    assert "no `claude` CLI" in review.commentary.lower() \
        or "disabled" in review.commentary.lower()


def test_letter_route_renders(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "shh")
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "none")
    accts, liabs, txns = make_sample_dataset()
    accts[0].current_balance = 6000.0
    accts[0].credit_limit = 5000.0
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)
    app = create_app(plaid=fake)
    client = TestClient(app)
    client.post("/exchange", data={"public_token": "p1"})

    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    client.post(f"/scan/{accts[0].account_id}")
    findings = scan_account(accts[0].account_id)
    over_limit = next(f for f in findings
                      if f.rule_id == "card_act.over_limit_balance")

    resp = client.get(f"/letter/{over_limit.finding_id}")
    assert resp.status_code == 200
    assert "Letter preview" in resp.text
    assert "Ian Test" in resp.text

    resp = client.post(f"/letter/{over_limit.finding_id}/save",
                       follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    resp = client.get(location)
    assert resp.status_code == 200

    resp = client.get(f"/letter/{over_limit.finding_id}/text")
    assert resp.status_code == 200
    assert "Ian Test" in resp.text
    assert "attachment" in resp.headers.get("content-disposition", "")


def test_settings_page_round_trip(monkeypatch):
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    resp = client.post("/settings", data={
        "name": "Ian Test", "address_line1": "1 Main",
        "city": "Austin", "state": "tx", "zip": "78701",
    }, follow_redirects=False)
    assert resp.status_code == 303
    resp = client.get("/settings")
    assert "Ian Test" in resp.text
    # State coerced to upper.
    assert 'value="TX"' in resp.text
