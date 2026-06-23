"""Phase 9: FCRA auto-firing rules + scan diagnostics."""
from __future__ import annotations

from datetime import date, timedelta

from lukav.collections_engine import (
    add_collection, audit_diagnostics, scan_collection,
)


def _seed(**overrides) -> str:
    payload = {
        "collector_name": "Test Collector",
        "collector_address": "",
        "original_creditor": "Test Bank",
        "alleged_amount": 500.00,
        "status": "in_collection",
        "first_contact_date": None,
        "last_activity_date": None,
        "state": "",
        "account_mask": "1234",
        "notes": "",
    }
    payload.update(overrides)
    return add_collection(payload)


def test_imported_tradeline_fires_fcra_bureau_dispute():
    coll_id = _seed()
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fcra.bureau_dispute_opportunity" for f in findings)


def test_imported_tradeline_fires_direct_furnisher_dispute():
    coll_id = _seed()
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fcra.direct_furnisher_dispute" for f in findings)


def test_paid_status_does_not_fire_fcra_rules():
    coll_id = _seed(status="paid")
    findings = scan_collection(coll_id)
    rule_ids = {f.rule_id for f in findings}
    assert "fcra.bureau_dispute_opportunity" not in rule_ids
    assert "fcra.direct_furnisher_dispute" not in rule_ids


def test_charged_off_fires_both_fcra_rules():
    coll_id = _seed(status="charged_off")
    findings = scan_collection(coll_id)
    rule_ids = {f.rule_id for f in findings}
    assert "fcra.bureau_dispute_opportunity" in rule_ids
    assert "fcra.direct_furnisher_dispute" in rule_ids


def test_diagnostic_flags_missing_state():
    coll_id = _seed(state="", last_activity_date="2024-01-01")
    diag = audit_diagnostics(coll_id)
    notes = " ".join(diag["notes"])
    assert "State of residence is empty" in notes


def test_diagnostic_flags_missing_last_activity():
    coll_id = _seed(state="TX", last_activity_date=None)
    diag = audit_diagnostics(coll_id)
    notes = " ".join(diag["notes"])
    assert "Last activity date is empty" in notes


def test_diagnostic_flags_missing_first_contact_for_collection_account():
    coll_id = _seed(status="in_collection", first_contact_date=None)
    diag = audit_diagnostics(coll_id)
    notes = " ".join(diag["notes"])
    assert "First-contact date is empty" in notes


def test_diagnostic_flags_missing_communications():
    coll_id = _seed()
    diag = audit_diagnostics(coll_id)
    notes = " ".join(diag["notes"])
    assert "Communications log is empty" in notes


def test_diagnostic_silent_when_fully_populated():
    from lukav.collections_engine import add_communication
    coll_id = _seed(
        state="TX",
        last_activity_date=(date.today() - timedelta(days=365 * 2)).isoformat(),
        first_contact_date=(date.today() - timedelta(days=30)).isoformat(),
    )
    add_communication(coll_id, {
        "kind": "letter",
        "occurred_at": "2025-06-01T00:00",
        "summary": "letter",
        "mini_miranda_present": "yes",
    })
    diag = audit_diagnostics(coll_id)
    assert diag["notes"] == []
