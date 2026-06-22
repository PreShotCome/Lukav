"""Phase 6 rule tests: Reg F 7-in-7, 7-day post-conversation, Mini-Miranda,
FCRA §1681c obsolete information, state-law extensions (Rosenthal etc.)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from lukav.collections_engine import (
    add_collection, add_communication, render_collection_letter,
    scan_collection,
)
from lukav.dispute_engine import save_profile


def _make_coll(**overrides) -> str:
    payload = {
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina, San Diego, CA 92108",
        "original_creditor": "Capital One",
        "alleged_amount": 1500.00,
        "status": "in_collection",
        "first_contact_date": date.today().isoformat(),
        "last_activity_date": (date.today() - timedelta(days=365 * 3)).isoformat(),
        "state": "TX",
        "account_mask": "4242",
        "notes": "",
    }
    payload.update(overrides)
    return add_collection(payload)


# ---- Reg F 7-in-7 -------------------------------------------------------

def test_reg_f_seven_in_seven_flagged_at_eight_calls():
    coll_id = _make_coll()
    base = datetime(2025, 6, 1, 10, 0)
    for i in range(8):
        add_communication(coll_id, {
            "kind": "phone",
            "occurred_at": (base + timedelta(days=i)).isoformat(),
            "summary": f"call {i}",
        })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.reg_f_seven_in_seven_calls" for f in findings)


def test_reg_f_seven_in_seven_not_flagged_at_seven_calls():
    coll_id = _make_coll()
    base = datetime(2025, 6, 1, 10, 0)
    for i in range(7):
        add_communication(coll_id, {
            "kind": "phone",
            "occurred_at": (base + timedelta(days=i)).isoformat(),
            "summary": f"call {i}",
        })
    findings = scan_collection(coll_id)
    assert not any(f.rule_id == "fdcpa.reg_f_seven_in_seven_calls" for f in findings)


# ---- Reg F 7-day post-conversation --------------------------------------

def test_reg_f_seven_day_post_conversation_flagged():
    coll_id = _make_coll()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 1, 10, 0).isoformat(),
        "summary": "Spoke with agent about settlement options",
    })
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 4, 10, 0).isoformat(),  # 3 days later
        "summary": "Another call",
    })
    findings = scan_collection(coll_id)
    assert any(
        f.rule_id == "fdcpa.reg_f_seven_days_after_conversation"
        for f in findings
    )


def test_reg_f_seven_day_window_not_flagged_after_8_days():
    coll_id = _make_coll()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 1, 10, 0).isoformat(),
        "summary": "Spoke with agent",
    })
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 10, 10, 0).isoformat(),  # 9 days later
        "summary": "Another call",
    })
    findings = scan_collection(coll_id)
    assert not any(
        f.rule_id == "fdcpa.reg_f_seven_days_after_conversation"
        for f in findings
    )


# ---- Mini-Miranda -------------------------------------------------------

def test_mini_miranda_missing_flagged_on_first_letter():
    coll_id = _make_coll()
    add_communication(coll_id, {
        "kind": "letter",
        "occurred_at": datetime(2025, 6, 1, 0, 0).isoformat(),
        "summary": "Demand letter",
        "mini_miranda_present": "no",
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.mini_miranda_required" for f in findings)


def test_mini_miranda_unknown_does_not_flag():
    coll_id = _make_coll()
    add_communication(coll_id, {
        "kind": "letter",
        "occurred_at": datetime(2025, 6, 1, 0, 0).isoformat(),
        "summary": "Demand letter",
        # leave mini_miranda_present unset -> stored as -1 (unknown)
    })
    findings = scan_collection(coll_id)
    assert not any(f.rule_id == "fdcpa.mini_miranda_required" for f in findings)


def test_mini_miranda_present_does_not_flag():
    coll_id = _make_coll()
    add_communication(coll_id, {
        "kind": "letter",
        "occurred_at": datetime(2025, 6, 1, 0, 0).isoformat(),
        "summary": "Demand letter with proper disclosure",
        "mini_miranda_present": "yes",
    })
    findings = scan_collection(coll_id)
    assert not any(f.rule_id == "fdcpa.mini_miranda_required" for f in findings)


# ---- FCRA §1681c obsolete information ----------------------------------

def test_obsolete_information_flagged_past_seven_years():
    coll_id = _make_coll(
        last_activity_date=(date.today() - timedelta(days=365 * 9)).isoformat(),
    )
    findings = scan_collection(coll_id)
    f = next((x for x in findings if x.rule_id == "fcra.obsolete_information"),
             None)
    assert f is not None
    assert f.evidence["years_past_expiry"] >= 1


def test_obsolete_information_not_flagged_at_five_years():
    coll_id = _make_coll(
        last_activity_date=(date.today() - timedelta(days=365 * 5)).isoformat(),
    )
    findings = scan_collection(coll_id)
    assert not any(f.rule_id == "fcra.obsolete_information" for f in findings)


# ---- State-law extensions ------------------------------------------------

def test_rosenthal_act_fires_for_ca():
    coll_id = _make_coll(state="CA")
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "state.rosenthal_act" for f in findings)


def test_texas_finance_392_fires_for_tx():
    coll_id = _make_coll(state="TX")
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "state.texas_finance_chapter_392" for f in findings)


def test_state_rules_do_not_fire_for_other_states():
    coll_id = _make_coll(state="OH")
    findings = scan_collection(coll_id)
    state_findings = [f for f in findings if f.rule_id.startswith("state.")]
    assert state_findings == []


# ---- Letter rendering --------------------------------------------------

def test_obsolete_info_removal_letter_renders():
    coll_id = _make_coll(
        last_activity_date=(date.today() - timedelta(days=365 * 8)).isoformat(),
    )
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    findings = scan_collection(coll_id)
    f = next(x for x in findings if x.rule_id == "fcra.obsolete_information")
    body = render_collection_letter(coll_id, "obsolete_info_removal.j2",
                                    finding_id=f.finding_id)
    assert body is not None
    assert "Ian Test" in body
    assert "§1681c" in body or "1681c" in body
    assert "Delete this item" in body
    # "seven (7)" + "years" — template line-wraps between them.
    assert "seven (7)" in body
    assert "years from the date" in body


def test_every_phase6_finding_has_a_citation():
    coll_id = _make_coll(
        state="CA",  # Rosenthal
        last_activity_date=(date.today() - timedelta(days=365 * 8)).isoformat(),
    )
    add_communication(coll_id, {
        "kind": "letter",
        "occurred_at": datetime(2025, 6, 1).isoformat(),
        "summary": "letter",
        "mini_miranda_present": "no",
    })
    for i in range(8):
        add_communication(coll_id, {
            "kind": "phone",
            "occurred_at": (datetime(2025, 7, 1) + timedelta(days=i)).isoformat(),
            "summary": f"call {i}",
        })
    findings = scan_collection(coll_id)
    assert findings
    for f in findings:
        assert f.citation, f"finding {f.rule_id} missing citation"
