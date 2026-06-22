"""Phase 4 collections tests: data model, communication log, audit rules,
letter rendering, and full route flow."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from lukav.collections_engine import (
    PICKABLE_TEMPLATES, add_collection, add_communication,
    get_collection, list_collection_findings, list_communications,
    render_collection_letter, scan_collection,
)
from lukav.dispute_engine import save_profile
from lukav.tests.fakes import FakePlaid
from lukav.web.app import create_app


def _seed_simple_collection(**overrides) -> str:
    payload = {
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina, San Diego CA 92108",
        "original_creditor": "Capital One",
        "alleged_amount": 1234.56,
        "status": "in_collection",
        "first_contact_date": date.today().isoformat(),
        "last_activity_date": (date.today() - timedelta(days=365 * 8)).isoformat(),
        "state": "TX",
        "account_mask": "4242",
        "notes": "",
    }
    payload.update(overrides)
    return add_collection(payload)


def test_collection_round_trip():
    coll_id = _seed_simple_collection()
    coll = get_collection(coll_id)
    assert coll is not None
    assert coll.collector_name == "Midland Credit Management"
    assert coll.state == "TX"
    assert coll.alleged_amount == 1234.56


def test_validation_opportunity_when_recent_contact():
    coll_id = _seed_simple_collection(
        first_contact_date=(date.today() - timedelta(days=10)).isoformat(),
    )
    findings = scan_collection(coll_id)
    f = next((x for x in findings if x.rule_id == "fdcpa.validation_opportunity"),
             None)
    assert f is not None
    assert f.evidence["within_30_day_window"] is True


def test_time_barred_violation_flagged():
    coll_id = _seed_simple_collection()  # TX, last activity 8y ago, SOL ~4y
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.time_barred_debt" for f in findings)


def test_outside_hours_phone_call_flagged():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 22, 15).isoformat(),  # 10:15 PM
        "summary": "Called late asking for payment.",
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.communication_outside_hours" for f in findings)


def test_third_party_disclosure_flagged():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 10, 0).isoformat(),
        "summary": "Spoke to my sister about the debt.",
        "third_party_disclosed": True,
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.third_party_disclosure" for f in findings)


def test_workplace_call_flagged():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 11, 0).isoformat(),
        "summary": "Called my office.",
        "called_at_workplace": True,
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.workplace_communication" for f in findings)


def test_harassment_flagged_on_profanity():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 10, 0).isoformat(),
        "summary": "Used profanity.",
        "profanity_or_abuse": True,
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.harassment" for f in findings)


def test_harassment_flagged_on_many_calls_one_day():
    coll_id = _seed_simple_collection()
    for hr in (9, 10, 11, 14):
        add_communication(coll_id, {
            "kind": "phone",
            "occurred_at": datetime(2025, 6, 12, hr, 0).isoformat(),
            "summary": "Call.",
        })
    findings = scan_collection(coll_id)
    repeated = [f for f in findings if f.title.startswith("Repeated phone")]
    assert repeated


def test_threat_on_time_barred_flagged():
    coll_id = _seed_simple_collection()  # time-barred TX
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 10, 0).isoformat(),
        "summary": "Threatened to sue.",
        "threat_of_suit": True,
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.threat_on_time_barred" for f in findings)


def test_contact_after_cease_flagged():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 10, 0).isoformat(),
        "summary": "Called after I sent the cease letter.",
        "after_cease_demand": True,
    })
    findings = scan_collection(coll_id)
    assert any(f.rule_id == "fdcpa.contact_after_cease" for f in findings)


def test_every_collection_finding_has_a_citation():
    coll_id = _seed_simple_collection()
    add_communication(coll_id, {
        "kind": "phone",
        "occurred_at": datetime(2025, 6, 12, 6, 30).isoformat(),
        "summary": "Profane early call, threatened suit.",
        "threat_of_suit": True,
        "profanity_or_abuse": True,
        "third_party_disclosed": True,
        "called_at_workplace": True,
        "after_cease_demand": True,
    })
    findings = scan_collection(coll_id)
    assert findings
    for f in findings:
        assert f.citation, f"finding {f.rule_id} missing citation"


def test_render_validation_letter_includes_collector_and_creditor():
    coll_id = _seed_simple_collection()
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    body = render_collection_letter(coll_id, "collection_validation.j2")
    assert body is not None
    assert "Midland Credit Management" in body
    assert "Capital One" in body
    assert "$1234.56" in body
    assert "Ian Test" in body
    assert "Austin, TX 78701" in body


def test_render_pay_for_delete_includes_settlement_language():
    coll_id = _seed_simple_collection()
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    body = render_collection_letter(coll_id, "pay_for_delete.j2")
    assert body is not None
    assert "pay-for-delete" in body.lower() or "settlement" in body.lower()
    assert "delete the tradeline" in body


def test_pickable_templates_all_render():
    coll_id = _seed_simple_collection()
    save_profile({
        "name": "Ian Test", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })
    for template_name, _label in PICKABLE_TEMPLATES:
        body = render_collection_letter(coll_id, template_name)
        assert body and "Ian Test" in body, f"{template_name} failed to render"


def test_full_collection_flow_via_routes(monkeypatch):
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "none")
    app = create_app(plaid=FakePlaid())
    client = TestClient(app)

    # Profile.
    client.post("/settings", data={
        "name": "Ian Test", "address_line1": "1 Main",
        "city": "Austin", "state": "TX", "zip": "78701",
    }, follow_redirects=False)

    # Create collection.
    resp = client.post("/collections", data={
        "collector_name": "Midland",
        "collector_address": "350 Camino de la Reina",
        "original_creditor": "Capital One",
        "alleged_amount": "999.99",
        "status": "in_collection",
        "first_contact_date": date.today().isoformat(),
        "last_activity_date": (date.today() - timedelta(days=365 * 8)).isoformat(),
        "state": "TX",
        "account_mask": "4242",
        "notes": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    coll_id = resp.headers["location"].rsplit("/", 1)[-1]

    # Add a communication.
    resp = client.post(f"/collections/{coll_id}/communication", data={
        "kind": "phone",
        "occurred_at": "2025-06-12T22:30",
        "summary": "Late call, threatened suit",
        "threat_of_suit": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303

    # Scan.
    resp = client.post(f"/collections/{coll_id}/scan", follow_redirects=False)
    assert resp.status_code == 303

    # Detail page shows findings.
    resp = client.get(f"/collections/{coll_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Midland" in body
    assert "outside the 8:00 AM" in body or "outside permitted hours" in body.lower()
    assert "time-barred" in body.lower() or "Statute of Limitations" in body

    # Render a pay-for-delete letter.
    resp = client.get(f"/collections/{coll_id}/letter/pay_for_delete.j2")
    assert resp.status_code == 200
    assert "Ian Test" in resp.text
    assert "Midland" in resp.text

    # Save it.
    resp = client.post(
        f"/collections/{coll_id}/letter/pay_for_delete.j2/save",
        data={"finding_id": ""}, follow_redirects=False,
    )
    assert resp.status_code == 303

    # Download .txt.
    resp = client.get(f"/collections/{coll_id}/letter/pay_for_delete.j2/text")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "Ian Test" in resp.text
