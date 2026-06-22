"""scripts/smoke.py — driver the e2e harness invokes.

Walks the full Lukav loop without any real Plaid credentials by using
FakePlaid: link → exchange → sync → save profile → save context →
scan → generate over-limit letter → confirm letter saved.

Exits non-zero on any failure. The e2e harness depends on this passing
in addition to /healthz."""
from __future__ import annotations

import sys
from datetime import date, timedelta

from fastapi.testclient import TestClient

from lukav.tests.fakes import FakePlaid, make_sample_dataset
from lukav.web.app import create_app
from lukav.audit_engine import scan_account
from lukav.dispute_engine import save_profile


def main() -> int:
    accts, liabs, txns = make_sample_dataset()
    accts[0].current_balance = 6000.0
    accts[0].credit_limit = 5000.0
    fake = FakePlaid(accounts=accts, liabilities=liabs, transactions=txns)

    # Disable LLM so smoke runs offline; settings-screen value of "TX" is
    # the trigger for the time-barred check.
    import os
    os.environ["LUKAV_LLM_BACKEND"] = "none"
    os.environ["PLAID_CLIENT_ID"] = "id"
    os.environ["PLAID_SECRET"] = "shh"

    app = create_app(plaid=fake)
    client = TestClient(app)

    # 1. Exchange — Plaid Item persisted, accounts/liabilities synced.
    resp = client.post("/exchange", data={"public_token": "smoke-pk"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["synced"]["accounts"] == 1

    # 2. Save user profile (needed for letter generation).
    save_profile({
        "name": "Smoke Tester", "address_line1": "1 Main St",
        "city": "Austin", "state": "TX", "zip": "78701",
    })

    # 3. Save manual debt context — TX state, ancient last-activity to
    #    trigger the time-barred SOL finding.
    long_ago = (date.today() - timedelta(days=365 * 10)).isoformat()
    resp = client.post(
        f"/scan/{accts[0].account_id}/context",
        data={
            "state": "TX",
            "last_activity_date": long_ago,
            "collection_letter_received": "1",
            "collection_letter_date": date.today().isoformat(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # 4. Run scan + verify findings.
    findings = scan_account(accts[0].account_id)
    assert findings, "scan produced no findings"
    rule_ids = {f.rule_id for f in findings}
    expected = {
        "card_act.over_limit_balance",
        "fdcpa.time_barred_debt",
        "fdcpa.validation_opportunity",
    }
    missing = expected - rule_ids
    assert not missing, f"missing expected findings: {missing}"

    # 5. Render and save a letter for the over-limit finding.
    over_limit = next(f for f in findings
                      if f.rule_id == "card_act.over_limit_balance")
    resp = client.get(f"/letter/{over_limit.finding_id}")
    assert resp.status_code == 200
    assert "Smoke Tester" in resp.text

    resp = client.post(f"/letter/{over_limit.finding_id}/save",
                       follow_redirects=False)
    assert resp.status_code == 303

    # 6. The letter shows on the index.
    resp = client.get("/letters")
    assert resp.status_code == 200
    assert "billing_error_notice.j2" in resp.text

    # 7. Phase 4: manual collection account + comms + scan + letter.
    long_ago_8y = (date.today() - timedelta(days=365 * 8)).isoformat()
    resp = client.post("/collections", data={
        "collector_name": "Midland Credit Management",
        "collector_address": "350 Camino de la Reina",
        "original_creditor": "Capital One",
        "alleged_amount": "1234.56",
        "status": "in_collection",
        "first_contact_date": date.today().isoformat(),
        "last_activity_date": long_ago_8y,
        "state": "TX",
        "account_mask": "4242",
    }, follow_redirects=False)
    assert resp.status_code == 303
    coll_id = resp.headers["location"].rsplit("/", 1)[-1]

    client.post(f"/collections/{coll_id}/communication", data={
        "kind": "phone",
        "occurred_at": "2025-06-12T22:30",
        "summary": "Threatened suit",
        "threat_of_suit": "1",
    })
    client.post(f"/collections/{coll_id}/scan")

    resp = client.get(f"/collections/{coll_id}")
    assert resp.status_code == 200
    for needle in ("Midland", "time-barred", "outside"):
        assert needle.lower() in resp.text.lower(), f"missing {needle!r}"

    resp = client.post(
        f"/collections/{coll_id}/letter/pay_for_delete.j2/save",
        data={"finding_id": ""}, follow_redirects=False,
    )
    assert resp.status_code == 303

    print("smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
