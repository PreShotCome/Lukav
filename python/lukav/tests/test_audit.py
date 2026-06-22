"""Discrepancy + FDCPA/FCRA audit tests."""
from __future__ import annotations

from datetime import date, timedelta

from lukav.audit_engine import list_findings, save_context, scan_account
from lukav.models.debt_models import (
    Account, Apr, Item, Liability, Transaction,
)
from lukav.storage import db
from lukav.tools.debt_audit import audit_account
from lukav.tools.fdcpa_fcra import audit_violations


def _seed_basic_account(
    *, balance: float = 2300.0, limit: float = 5000.0,
    aprs: list[Apr] | None = None,
    min_payment: float = 75.0, statement_balance: float = 2300.0,
    last_payment_date: date | None = None,
) -> Account:
    db.init_db()
    db.upsert_item(Item(item_id="i1", institution_name="Test Bank", access_token="x"))
    acct = Account(
        account_id="acct-1", item_id="i1",
        name="Test Visa", official_name=None, mask="4242", subtype="credit card",
        current_balance=balance, available_balance=limit - balance,
        credit_limit=limit, iso_currency_code="USD",
    )
    db.upsert_account(acct)
    liab = Liability(
        account_id=acct.account_id,
        last_payment_date=last_payment_date,
        last_statement_balance=statement_balance,
        minimum_payment_amount=min_payment,
        aprs=aprs or [],
    )
    db.upsert_liability(liab)
    return acct


def test_interest_charge_implied_apr_flagged_when_too_high():
    # Disclosed 12% APR, but interest implies ~24% APR — should flag.
    acct = _seed_basic_account(aprs=[
        Apr(apr_percentage=12.0, apr_type="purchase_apr",
            balance_subject_to_apr=1000.0,
            interest_charge_amount=20.0),       # implied APR = 24%
    ])
    findings = audit_account(acct.account_id)
    assert any(f.rule_id == "card_act.interest_charge_implied_apr" for f in findings)
    f = next(f for f in findings if f.rule_id == "card_act.interest_charge_implied_apr")
    assert f.evidence["disclosed_apr_pct"] == 12.0
    assert f.evidence["implied_apr_pct"] > 23.0
    assert "1666" not in f.citation   # this finding uses 1637, not 1666
    assert f.citation


def test_interest_charge_within_tolerance_does_not_flag():
    # 24% disclosed, interest = balance * 24/100 / 12 = 20.00 → exactly matches.
    acct = _seed_basic_account(aprs=[
        Apr(apr_percentage=24.0, apr_type="purchase_apr",
            balance_subject_to_apr=1000.0, interest_charge_amount=20.0),
    ])
    findings = audit_account(acct.account_id)
    assert not any(f.rule_id == "card_act.interest_charge_implied_apr" for f in findings)


def test_over_limit_balance_flagged():
    acct = _seed_basic_account(balance=5500.0, limit=5000.0)
    findings = audit_account(acct.account_id)
    assert any(f.rule_id == "card_act.over_limit_balance" for f in findings)


def test_late_fee_above_first_cap_flagged():
    acct = _seed_basic_account()
    today = date.today()
    db.upsert_transaction(Transaction(
        transaction_id="t-late-1", account_id=acct.account_id,
        posted_date=today - timedelta(days=10),
        amount=39.00, name="LATE PAYMENT FEE",
    ))
    findings = audit_account(acct.account_id)
    flagged = [f for f in findings if f.rule_id == "card_act.first_late_fee_cap"]
    assert len(flagged) == 1
    assert flagged[0].evidence["amount"] == 39.00


def test_two_close_late_fees_treats_second_as_repeat():
    acct = _seed_basic_account()
    today = date.today()
    db.upsert_transaction(Transaction(
        transaction_id="t-late-1", account_id=acct.account_id,
        posted_date=today - timedelta(days=60),
        amount=33.00, name="LATE PAYMENT FEE",
    ))
    db.upsert_transaction(Transaction(
        transaction_id="t-late-2", account_id=acct.account_id,
        posted_date=today - timedelta(days=30),
        amount=45.00, name="LATE PAYMENT FEE",
    ))
    findings = audit_account(acct.account_id)
    flagged_rules = {f.rule_id for f in findings}
    assert "card_act.first_late_fee_cap" in flagged_rules
    assert "card_act.repeat_late_fee_cap" in flagged_rules


def test_over_limit_fee_always_flagged():
    acct = _seed_basic_account()
    db.upsert_transaction(Transaction(
        transaction_id="t-ol", account_id=acct.account_id,
        posted_date=date.today() - timedelta(days=5),
        amount=25.0, name="Over-limit Fee",
    ))
    findings = audit_account(acct.account_id)
    assert any(f.rule_id == "card_act.over_limit_fee_requires_opt_in" for f in findings)


def test_duplicate_charges_flagged_within_3_days():
    acct = _seed_basic_account()
    base = date.today() - timedelta(days=15)
    db.upsert_transaction(Transaction(
        transaction_id="t-dup-1", account_id=acct.account_id,
        posted_date=base, amount=42.13, name="Coffee Shop",
        merchant_name="Coffee Shop",
    ))
    db.upsert_transaction(Transaction(
        transaction_id="t-dup-2", account_id=acct.account_id,
        posted_date=base + timedelta(days=2), amount=42.13, name="Coffee Shop",
        merchant_name="Coffee Shop",
    ))
    findings = audit_account(acct.account_id)
    assert any(f.rule_id == "billing.possible_duplicate_charge" for f in findings)


def test_minimum_payment_greater_than_balance_flagged():
    acct = _seed_basic_account(min_payment=300.0, statement_balance=200.0)
    findings = audit_account(acct.account_id)
    assert any(f.rule_id == "card_act.min_payment_exceeds_balance" for f in findings)


def test_time_barred_debt_violation_flagged():
    long_ago = date.today() - timedelta(days=365 * 10)
    acct = _seed_basic_account(last_payment_date=long_ago)
    findings = audit_violations(acct.account_id, context={
        "state": "TX",  # 4-year SOL
        "last_activity_date": long_ago.isoformat(),
    })
    assert any(f.rule_id == "fdcpa.time_barred_debt" for f in findings)


def test_validation_opportunity_flagged_when_letter_received():
    acct = _seed_basic_account()
    findings = audit_violations(acct.account_id, context={
        "collection_letter_received": True,
        "collection_letter_date": (date.today() - timedelta(days=10)).isoformat(),
    })
    f = next((x for x in findings if x.rule_id == "fdcpa.validation_opportunity"),
             None)
    assert f is not None
    assert f.evidence["within_30_day_window"] is True


def test_scan_persists_findings_and_lists_them():
    acct = _seed_basic_account(balance=5500.0, limit=5000.0)
    findings = scan_account(acct.account_id)
    assert findings, "scan should produce at least one finding"
    listed = list_findings(acct.account_id)
    assert {f.finding_id for f in listed} == {f.finding_id for f in findings}


def test_scan_is_idempotent():
    acct = _seed_basic_account(balance=5500.0, limit=5000.0)
    a = scan_account(acct.account_id)
    b = scan_account(acct.account_id)
    assert len(a) == len(b)
    # Rule IDs are stable across runs even if finding_ids are not.
    assert {f.rule_id for f in a} == {f.rule_id for f in b}


def test_no_finding_is_uncited():
    acct = _seed_basic_account(
        balance=5500.0, limit=5000.0,
        aprs=[Apr(apr_percentage=12.0, apr_type="purchase_apr",
                  balance_subject_to_apr=1000.0, interest_charge_amount=20.0)],
    )
    save_context(acct.account_id, {
        "state": "TX",
        "last_activity_date": (date.today() - timedelta(days=365 * 10)).isoformat(),
        "collection_letter_received": True,
        "collection_letter_date": date.today().isoformat(),
        "credit_report_dispute_basis": "Reported as charge-off when paid in full",
    })
    findings = scan_account(acct.account_id)
    assert findings
    for f in findings:
        assert f.citation, f"finding {f.rule_id} missing citation"
