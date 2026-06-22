"""Map a Finding's rule_id to the right dispute-letter template, then
render it with the user's profile."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lukav.audit_engine import get_finding
from lukav.models.debt_models import Account
from lukav.models.findings import Finding
from lukav.storage import db
from lukav.storage.db import connect as _connect

_TEMPLATE_DIR = Path(__file__).resolve().parent / "legal" / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(default=False),  # plain text, not HTML
    trim_blocks=False,
    lstrip_blocks=False,
)

# rule_id → letter template. Multiple rule_ids can resolve to the same
# template (e.g. several CARD-Act fee discrepancies all use the FCBA
# billing-error notice).
RULE_TO_TEMPLATE: dict[str, str] = {
    # Discrepancies (CARD Act, billing) — FCBA billing-error notice.
    "card_act.interest_charge_implied_apr": "billing_error_notice.j2",
    "card_act.first_late_fee_cap":          "billing_error_notice.j2",
    "card_act.repeat_late_fee_cap":         "billing_error_notice.j2",
    "card_act.over_limit_fee_requires_opt_in": "billing_error_notice.j2",
    "card_act.over_limit_balance":          "billing_error_notice.j2",
    "card_act.min_payment_exceeds_balance": "billing_error_notice.j2",
    "billing.possible_duplicate_charge":    "billing_error_notice.j2",
    # FDCPA / FCRA violations.
    "fdcpa.validation_opportunity":     "debt_validation.j2",
    "fdcpa.time_barred_debt":           "cease_contact.j2",
    "fdcpa.cease_contact":              "cease_contact.j2",
    "fcra.bureau_dispute_opportunity":  "fcra_dispute.j2",
    "fcra.direct_furnisher_dispute":    "direct_dispute.j2",
}


PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT NOT NULL DEFAULT '',
    address_line1   TEXT NOT NULL DEFAULT '',
    address_line2   TEXT NOT NULL DEFAULT '',
    city            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT '',
    zip             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS letters (
    letter_id   TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    template    TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def init_letters() -> None:
    with _connect() as conn:
        conn.executescript(PROFILE_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO profile (id) VALUES (1)"
        )


def get_profile() -> dict:
    init_letters()
    with _connect() as conn:
        r = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return dict(r) if r else {}


def save_profile(payload: dict) -> None:
    init_letters()
    fields = ("name", "address_line1", "address_line2",
              "city", "state", "zip")
    values = [payload.get(f, "") for f in fields]
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE profile SET
              name = ?, address_line1 = ?, address_line2 = ?,
              city = ?, state = ?, zip = ?
            WHERE id = 1
            """,
            values,
        )


def template_for(finding: Finding) -> Optional[str]:
    return RULE_TO_TEMPLATE.get(finding.rule_id)


def render_letter(finding_id: str) -> Optional[str]:
    finding = get_finding(finding_id)
    if not finding:
        return None
    template_name = template_for(finding)
    if not template_name:
        return None
    account = db.get_account(finding.account_id)
    if not account:
        return None
    profile = get_profile()
    template = _env.get_template(template_name)
    body = template.render(
        profile=profile,
        recipient=None,
        finding=finding,
        account=account,
        today=date.today().strftime("%B %d, %Y"),
    )
    return body


def save_letter(finding_id: str, body: str, template_name: str) -> str:
    finding = get_finding(finding_id)
    assert finding is not None
    import uuid
    letter_id = f"letter-{uuid.uuid4().hex[:12]}"
    from lukav.models.findings import now_iso
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO letters (letter_id, finding_id, account_id,
                                 template, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (letter_id, finding_id, finding.account_id, template_name,
             body, now_iso()),
        )
    return letter_id


def list_letters() -> list[dict]:
    init_letters()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT letter_id, finding_id, account_id, template, created_at "
            "FROM letters ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_letter(letter_id: str) -> Optional[dict]:
    init_letters()
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM letters WHERE letter_id = ?", (letter_id,),
        ).fetchone()
    return dict(r) if r else None
