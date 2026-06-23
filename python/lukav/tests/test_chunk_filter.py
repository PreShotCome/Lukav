"""Tests for the chunk pre-filter + new credit-report chunk pipeline."""
from __future__ import annotations

from lukav.ingest_credit_report import (
    _chunk_is_relevant, _chunk_text, extract_credit_report,
)
from lukav.tests.test_phase8 import FakeLLM, SAMPLE_REPORT_TEXT


def test_chunk_with_collection_keyword_is_relevant():
    assert _chunk_is_relevant("Status: Collection account") is True


def test_chunk_with_charge_off_is_relevant():
    assert _chunk_is_relevant("Account closed - Charge-off") is True


def test_personal_info_chunk_is_not_relevant():
    text = (
        "Personal Information\nIAN LEE\n123 Main St\nAustin, TX 78701\n"
        "SSN Variations\nEmployers: ACME CORP\n"
    )
    assert _chunk_is_relevant(text) is False


def test_inquiries_chunk_is_not_relevant():
    text = (
        "Hard Inquiries\nAPPLE CARD - 2024-08-15\n"
        "DISCOVER - 2024-09-20\nAMEX - 2024-10-01\n"
    )
    assert _chunk_is_relevant(text) is False


def test_extract_skips_irrelevant_chunks():
    """LLM should not be called on chunks without any negative keyword."""
    fake = FakeLLM([
        {"collector_name": "Midland Credit Management",
         "original_creditor": "Capital One", "alleged_amount": 1234.56,
         "account_mask": "4242", "date_opened": None,
         "date_of_first_delinquency": None, "last_activity_date": None,
         "status": "in_collection", "bureau": "Equifax", "notes": ""},
    ])
    # Pad the sample report with a lot of irrelevant text. The pre-filter
    # should still pick out the negative section.
    padded = ("PERSONAL INFORMATION\nIAN LEE\n" * 200) + SAMPLE_REPORT_TEXT
    result = extract_credit_report(padded, llm_client=fake)
    assert len(result.tradelines) == 1
    assert result.tradelines[0].collector_name == "Midland Credit Management"


def test_extract_reports_when_no_relevant_chunks():
    fake = FakeLLM([])
    clean_text = "PERSONAL INFORMATION\nIAN LEE\nALL ACCOUNTS PAID AS AGREED\n" * 50
    result = extract_credit_report(clean_text, llm_client=fake)
    assert result.tradelines == []
    assert "no chunks contained" in (result.error or "").lower() \
        or "negative-account" in (result.error or "").lower()
