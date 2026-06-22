"""Consumer-protection violation flagger.

v1 detects what we can from Plaid + the per-account manual context
(state, collector name, first-contact date, collection notice received).
The remaining FDCPA/FCRA rules need data Plaid does not expose; those
become available when the user fills the manual-context form (Phase 3)
or when Lukav adds credit-report ingestion."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Iterable

from lukav.legal.rules import load_rules
from lukav.models.debt_models import Account, Liability
from lukav.models.findings import Finding, now_iso
from lukav.storage import db
from lukav.tools.base import Tool, ToolRegistry

# State statute-of-limitations on credit card debt (years), starting
# point — extend as needed. Source: state-by-state consumer law summaries
# (e.g. NCLC). Values are typical; the dispute letter itself cites the
# state code, not this table.
STATE_SOL_YEARS: dict[str, int] = {
    "AL": 3, "AK": 3, "AZ": 6, "AR": 5, "CA": 4, "CO": 6, "CT": 6,
    "DE": 4, "DC": 3, "FL": 5, "GA": 6, "HI": 6, "ID": 5, "IL": 5,
    "IN": 6, "IA": 5, "KS": 3, "KY": 5, "LA": 3, "ME": 6, "MD": 3,
    "MA": 6, "MI": 6, "MN": 6, "MS": 3, "MO": 5, "MT": 5, "NE": 5,
    "NV": 4, "NH": 3, "NJ": 6, "NM": 4, "NY": 3, "NC": 3, "ND": 6,
    "OH": 6, "OK": 5, "OR": 6, "PA": 4, "RI": 10, "SC": 3, "SD": 6,
    "TN": 6, "TX": 4, "UT": 4, "VT": 6, "VA": 5, "WA": 6, "WV": 5,
    "WI": 6, "WY": 8,
}


def audit_violations(account_id: str, context: dict | None = None) -> list[Finding]:
    """Phase-2 implementation. `context` is the manual debt context the
    user supplies via the scan form (Phase 3 wires the UI; today the
    callers can pass it directly)."""
    account = db.get_account(account_id)
    if not account:
        return []
    liab = db.get_liability(account_id)
    fdcpa = load_rules("fdcpa")
    fcra = load_rules("fcra")
    ctx = context or {}

    findings: list[Finding] = []
    findings.extend(_flag_validation_opportunity(account, ctx, fdcpa))
    findings.extend(_flag_time_barred(account, liab, ctx, fdcpa))
    findings.extend(_flag_fcra_dispute_opportunity(account, ctx, fcra))
    return [f for f in findings if f.citation]


# ---- rule implementations ------------------------------------------------

def _new(rule_id: str) -> str:
    return f"{rule_id}.{uuid.uuid4().hex[:10]}"


def _flag_validation_opportunity(
    account: Account, ctx: dict, fdcpa: dict,
) -> Iterable[Finding]:
    if not ctx.get("collection_letter_received"):
        return
    rule = fdcpa["fdcpa.validation_opportunity"]
    received_str = ctx.get("collection_letter_date")
    received = _parse_date(received_str)
    days_since = (date.today() - received).days if received else None
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=account.account_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"You reported receiving a collection letter"
            + (f" on {received_str} ({days_since} days ago)" if days_since is not None else "")
            + ". Within 30 days of that first written communication you "
              "may send a written validation request, which suspends "
              "collection activity until validated."
        ),
        citation=rule["citation"],
        evidence={
            "collection_letter_received": True,
            "collection_letter_date": received_str,
            "days_since": days_since,
            "within_30_day_window": (days_since is not None and days_since <= 30),
        },
        created_at=now_iso(),
    )


def _flag_time_barred(
    account: Account, liab: Liability | None, ctx: dict, fdcpa: dict,
) -> Iterable[Finding]:
    state = (ctx.get("state") or "").upper().strip()
    last_activity = _parse_date(ctx.get("last_activity_date")) or (
        liab.last_payment_date if liab else None
    )
    if not state or state not in STATE_SOL_YEARS or last_activity is None:
        return
    sol_years = STATE_SOL_YEARS[state]
    expiry = date(
        last_activity.year + sol_years,
        last_activity.month, min(last_activity.day, 28),
    )
    if date.today() < expiry:
        return
    rule = fdcpa["fdcpa.time_barred_debt"]
    years_past = (date.today() - expiry).days // 365
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=account.account_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"Last activity on this account was {last_activity}. "
            f"{state}'s SOL on credit-card debt is approximately "
            f"{sol_years} years, which expired around {expiry} "
            f"({years_past} years ago). A collector cannot sue or "
            f"threaten suit, and certain communications must include the "
            f"CFPB time-barred disclosure."
        ),
        citation=rule["citation"],
        evidence={
            "state": state,
            "approx_sol_years": sol_years,
            "last_activity_date": last_activity.isoformat(),
            "approx_sol_expiry": expiry.isoformat(),
        },
        created_at=now_iso(),
    )


def _flag_fcra_dispute_opportunity(
    account: Account, ctx: dict, fcra: dict,
) -> Iterable[Finding]:
    if not ctx.get("credit_report_dispute_basis"):
        return
    rule = fcra["fcra.bureau_dispute_opportunity"]
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=account.account_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            "You flagged a basis to dispute how this account is being "
            "reported. You may file a §611 dispute with each bureau and a "
            "§623 direct dispute with the furnisher. Use fcra_dispute.j2."
        ),
        citation=rule["citation"],
        evidence={"basis": ctx.get("credit_report_dispute_basis")},
        created_at=now_iso(),
    )


# ---- helpers + registration ---------------------------------------------

def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _audit_tool(account_id: str, context: dict | None = None) -> list[dict]:
    return [f.__dict__ for f in audit_violations(account_id, context)]


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="audit_violations",
        description="Flag FDCPA / FCRA opportunities and violations for one "
                    "account, using the optional manual debt context (state, "
                    "collection-letter date, dispute basis).",
        parameters_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "context": {"type": "object",
                            "description": "Manual debt context."},
            },
            "required": ["account_id"],
        },
        handler=_audit_tool,
    ))
