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


def test_extract_processes_all_chunks_even_without_keywords():
    """Phase 11 change: removed the keyword-gate. Every chunk is now sent
    to the LLM. Verify the FakeLLM was called for chunks that have no
    negative-account keywords."""
    fake = FakeLLM([
        {"collector_name": "Midland Credit Management",
         "original_creditor": "Capital One", "alleged_amount": 1234.56,
         "account_mask": "4242", "date_opened": None,
         "date_of_first_delinquency": None, "last_activity_date": None,
         "status": "in_collection", "bureau": "Equifax", "notes": ""},
    ])
    padded = ("PERSONAL INFORMATION\nIAN LEE\n" * 200) + SAMPLE_REPORT_TEXT
    result = extract_credit_report(padded, llm_client=fake)
    # FakeLLM returns the Midland row for EVERY chunk — that's the whole
    # point. So processed > 1 confirms we didn't gate.
    assert result.chunks_processed >= 1
    assert result.chunks_total >= result.chunks_processed
    assert len(result.tradelines) == 1   # dedup keeps it to one


def test_extract_reports_chunk_counts_even_when_no_tradelines():
    fake = FakeLLM([])    # LLM returns no tradelines for any chunk
    clean_text = (
        "PERSONAL INFORMATION\nIAN LEE\nALL ACCOUNTS PAID AS AGREED\n" * 50
    )
    result = extract_credit_report(clean_text, llm_client=fake)
    assert result.tradelines == []
    assert result.chunks_processed >= 1
    assert result.chunks_total >= 1
