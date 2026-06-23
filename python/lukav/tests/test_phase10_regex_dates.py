"""Phase 10: tradeline-aware chunking + regex date fallback.

Tests the credit-report ingest's recovery from LLM date misses by
running the regex fallback over a synthetic credit-report block."""
from __future__ import annotations

from lukav.ingest_credit_report import (
    Tradeline, _chunk_text, _find_block_for_collector,
    _normalize_date, _regex_fill_dates, extract_credit_report,
)
from lukav.tests.test_phase8 import FakeLLM


REPORT_WITH_DATES = """\
EQUIFAX CREDIT REPORT
Personal Information omitted.

NEGATIVE ACCOUNTS
-----------------

MIDLAND CREDIT MGMT
Account Number: ****4242
Original Creditor: Capital One Bank
Date Opened: 06/15/2018
Date of First Delinquency: 12/15/2018
Date of Last Activity: 03/01/2019
Status: Collection account
Balance: $1,234.56

PORTFOLIO RECOVERY ASSOC
Account Number: ****9911
Original Creditor: Synchrony Bank
Opened: Jan 15, 2019
First Delinquency: July 2019
Last Reported: 2020-03-15
Status: Charge-off
Balance: $487.10
"""


def test_normalize_date_handles_common_formats():
    assert _normalize_date("06/15/2018") == "2018-06-15"
    assert _normalize_date("2018-06-15") == "2018-06-15"
    assert _normalize_date("Jun 15, 2018") == "2018-06-15"
    assert _normalize_date("June 15 2018") == "2018-06-15"
    assert _normalize_date("Jun 2018") == "2018-06-01"
    assert _normalize_date("June 2018") == "2018-06-01"
    assert _normalize_date("nonsense") is None


def test_find_block_for_collector_returns_surrounding_text():
    block = _find_block_for_collector(REPORT_WITH_DATES, "Midland Credit Management")
    assert "Date Opened: 06/15/2018" in block
    assert "Status: Collection" in block
    # Should not bleed past into the Portfolio block far away.
    assert block.count("Portfolio") <= 1


def test_regex_fills_dates_left_blank_by_llm():
    t = Tradeline(
        collector_name="Midland Credit Management",
        original_creditor="Capital One Bank",
        alleged_amount=1234.56,
        account_mask="4242",
        date_opened=None,
        date_of_first_delinquency=None,
        last_activity_date=None,
        status="in_collection",
    )
    _regex_fill_dates(t, REPORT_WITH_DATES)
    assert t.date_opened == "2018-06-15"
    assert t.date_of_first_delinquency == "2018-12-15"
    assert t.last_activity_date == "2019-03-01"


def test_regex_handles_alt_label_forms():
    """Portfolio Recovery section uses 'Opened:' (no 'Date') and
    'First Delinquency:' (no 'Date of'). Should still grab them."""
    t = Tradeline(
        collector_name="Portfolio Recovery Associates",
        alleged_amount=487.10,
        account_mask="9911",
        status="charged_off",
    )
    _regex_fill_dates(t, REPORT_WITH_DATES)
    assert t.date_opened == "2019-01-15"
    assert t.date_of_first_delinquency == "2019-07-01"
    # "Last Reported" maps to last_activity_date.
    assert t.last_activity_date == "2020-03-15"


def test_regex_does_not_overwrite_llm_supplied_date():
    """If the LLM already filled date_opened, regex leaves it alone."""
    t = Tradeline(
        collector_name="Midland Credit Management",
        date_opened="2020-01-01",
    )
    _regex_fill_dates(t, REPORT_WITH_DATES)
    assert t.date_opened == "2020-01-01"


def test_chunker_keeps_full_tradeline_in_one_chunk():
    big_report = "PERSONAL INFO " * 1500 + "\n\n" + REPORT_WITH_DATES
    chunks = _chunk_text(big_report)
    assert any("MIDLAND CREDIT MGMT" in c and "Date Opened: 06/15/2018" in c
               for c in chunks)


def test_extract_uses_regex_fallback_for_dates(monkeypatch):
    # LLM returns the tradeline WITHOUT any dates (forgot them).
    fake = FakeLLM([{
        "collector_name": "Midland Credit Management",
        "original_creditor": "Capital One Bank",
        "alleged_amount": 1234.56,
        "account_mask": "4242",
        "date_opened": None,
        "date_of_first_delinquency": None,
        "last_activity_date": None,
        "status": "in_collection",
        "bureau": "Equifax",
        "notes": "",
    }])
    result = extract_credit_report(REPORT_WITH_DATES, llm_client=fake)
    assert len(result.tradelines) == 1
    t = result.tradelines[0]
    assert t.date_opened == "2018-06-15"
    assert t.date_of_first_delinquency == "2018-12-15"
    assert t.last_activity_date == "2019-03-01"
