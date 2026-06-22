"""Storage round-trip tests. Confirms the SQLite schema preserves every
field the audit engine needs (APRs, statement balances, due dates)."""
from __future__ import annotations

from datetime import date

from lukav.models.debt_models import Item
from lukav.storage import db
from lukav.tests.fakes import make_sample_dataset


def test_init_db_is_idempotent():
    db.init_db()
    db.init_db()
    assert db.list_accounts() == []


def test_item_roundtrip():
    db.init_db()
    db.upsert_item(Item(item_id="item-1", institution_name="Test",
                        access_token="t", cursor=None, active=True))
    items = db.list_items()
    assert len(items) == 1
    assert items[0].institution_name == "Test"


def test_account_and_liability_roundtrip():
    db.init_db()
    db.upsert_item(Item(item_id="item-test-1", institution_name="Bank",
                        access_token="x"))
    accts, liabs, txns = make_sample_dataset()
    for a in accts:
        db.upsert_account(a)
    for l in liabs:
        db.upsert_liability(l)
    for t in txns:
        db.upsert_transaction(t)

    fetched_accts = db.list_accounts()
    assert len(fetched_accts) == 1
    assert fetched_accts[0].credit_limit == 5000.00

    liab = db.get_liability("acct-test-1")
    assert liab is not None
    assert liab.minimum_payment_amount == 75.00
    assert len(liab.aprs) == 2
    purchase = next(a for a in liab.aprs if a.apr_type == "purchase_apr")
    assert purchase.apr_percentage == 24.99
    assert purchase.interest_charge_amount == 47.18
    assert isinstance(liab.next_payment_due_date, date)

    listed_txns = db.list_transactions("acct-test-1")
    assert len(listed_txns) == 5


def test_upsert_account_updates_existing():
    db.init_db()
    accts, _, _ = make_sample_dataset()
    a = accts[0]
    db.upsert_item(Item(item_id=a.item_id, institution_name="B", access_token="x"))
    db.upsert_account(a)
    a.current_balance = 9999.99
    db.upsert_account(a)
    fetched = db.get_account(a.account_id)
    assert fetched is not None
    assert fetched.current_balance == 9999.99
