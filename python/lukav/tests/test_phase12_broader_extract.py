"""Phase 12: broader extraction (open-with-lates, closed-with-lates).
Also confirms dedup keys keep different debts from same collector."""
from __future__ import annotations

from lukav.collections_engine import (
    add_collection, scan_collection,
)
from lukav.ingest_credit_report import extract_credit_report
from lukav.tests.test_phase8 import FakeLLM


REPORT_MIXED = """\
EQUIFAX CREDIT REPORT

ACCOUNT 1: BIG BANK CREDIT CARD
Status: Open
Account Number: ****1111
Date Opened: 2020-01-15
Payment history: 2024-01 OK, 2023-12 30, 2023-11 OK
Balance: $500.00

ACCOUNT 2: MIDLAND CREDIT MGMT
Status: Collection account
Account Number: ****4242
Original Creditor: Capital One
Date Opened: 2018-06-15
Date of First Delinquency: 2018-12-15
Balance: $1,234.56

ACCOUNT 3: STORE CARD
Status: Closed
Account Number: ****7777
Date Opened: 2019-03-01
Date Closed: 2022-05-15
Worst delinquency: 90 days late
Balance: $0
"""


def test_open_account_with_late_history_is_extracted():
    fake = FakeLLM([
        {"collector_name": "Big Bank Credit Card",
         "original_creditor": "", "alleged_amount": 500.00,
         "account_mask": "1111", "date_opened": "2020-01-15",
         "date_of_first_delinquency": None,
         "last_activity_date": None,
         "status": "open_with_lates",
         "bureau": "Equifax", "notes": "30-day late in 2023-12"},
    ])
    result = extract_credit_report(REPORT_MIXED, llm_client=fake)
    assert len(result.tradelines) == 1
    assert result.tradelines[0].status == "open_with_lates"


def test_closed_with_lates_status_accepted():
    fake = FakeLLM([
        {"collector_name": "Store Card",
         "original_creditor": "", "alleged_amount": 0,
         "account_mask": "7777", "date_opened": "2019-03-01",
         "date_of_first_delinquency": None,
         "last_activity_date": None,
         "status": "closed_with_lates",
         "bureau": "Equifax", "notes": "worst delinquency 90 days"},
    ])
    result = extract_credit_report(REPORT_MIXED, llm_client=fake)
    assert len(result.tradelines) == 1
    assert result.tradelines[0].status == "closed_with_lates"


def test_open_with_lates_fires_fcra_dispute_rules():
    """The whole reason we extract them — they're still credit-repair targets."""
    coll_id = add_collection({
        "collector_name": "Big Bank Credit Card",
        "alleged_amount": 500.0,
        "status": "open_with_lates",
        "state": "TX",
    })
    findings = scan_collection(coll_id)
    rule_ids = {f.rule_id for f in findings}
    assert "fcra.bureau_dispute_opportunity" in rule_ids
    assert "fcra.direct_furnisher_dispute" in rule_ids


def test_dedup_keeps_same_collector_with_different_creditors():
    """Same collector name, different original creditors -> should both
    be kept (not deduplicated). Dedup key includes original_creditor."""
    fake = FakeLLM([
        {"collector_name": "Midland Credit Management",
         "original_creditor": "Capital One",
         "alleged_amount": 1234.56, "account_mask": "",
         "date_opened": None, "date_of_first_delinquency": None,
         "last_activity_date": None,
         "status": "in_collection", "bureau": "", "notes": ""},
        {"collector_name": "Midland Credit Management",
         "original_creditor": "Synchrony Bank",
         "alleged_amount": 487.10, "account_mask": "",
         "date_opened": None, "date_of_first_delinquency": None,
         "last_activity_date": None,
         "status": "in_collection", "bureau": "", "notes": ""},
    ])
    # Both names need to appear in source text for grounding to pass.
    text = (
        "MIDLAND CREDIT MGMT - Capital One - $1234.56\n"
        "MIDLAND CREDIT MGMT - Synchrony Bank - $487.10\n"
    )
    result = extract_credit_report(text, llm_client=fake)
    assert len(result.tradelines) == 2
    creditors = {t.original_creditor for t in result.tradelines}
    assert creditors == {"Capital One", "Synchrony Bank"}
