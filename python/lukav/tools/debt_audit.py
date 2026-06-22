"""Deterministic discrepancy / fee checks.

Reads the rule cap values from legal/rules/card_act.yaml so the cap
numbers are not duplicated in code. Every flagged finding carries:
  - rule_id (matches the YAML)
  - citation (copied from the YAML)
  - evidence (the math that produced the flag)

Findings without a citation are dropped — the scan UI never surfaces
unsourced claims."""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable

from lukav.legal.rules import load_rules
from lukav.models.debt_models import Account, Apr, Liability, Transaction
from lukav.models.findings import Finding, now_iso
from lukav.storage import db
from lukav.tools.base import Tool, ToolRegistry

LATE_FEE_RE = re.compile(r"late\s*(payment\s*)?fee", re.IGNORECASE)
OVER_LIMIT_FEE_RE = re.compile(r"over[\s\-]*limit\s*fee", re.IGNORECASE)


def _f(rule: dict, key: str) -> str:
    return str(rule.get(key, ""))


def _new(rule_id: str) -> str:
    return f"{rule_id}.{uuid.uuid4().hex[:10]}"


def audit_account(account_id: str) -> list[Finding]:
    """Run all deterministic checks for one account. Idempotent — does
    not write to the DB."""
    account = db.get_account(account_id)
    if not account:
        return []
    liab = db.get_liability(account_id)
    txns = db.list_transactions(account_id)
    rules = load_rules("card_act")

    findings: list[Finding] = []
    if liab:
        findings.extend(_check_interest_charge_apr(account, liab, rules))
        findings.extend(_check_min_payment(account, liab, rules))
    findings.extend(_check_over_limit(account, rules))
    findings.extend(_check_late_fees(account, txns, rules))
    findings.extend(_check_over_limit_fees(account, txns, rules))
    findings.extend(_check_duplicate_charges(account, txns))
    # Drop any finding that lost its citation; the scan UI must never
    # surface uncited claims.
    return [f for f in findings if f.citation]


def _check_interest_charge_apr(
    account: Account, liab: Liability, rules: dict,
) -> Iterable[Finding]:
    rule = rules.get("card_act.interest_charge_implied_apr")
    if not rule:
        return
    tolerance = float(rule.get("tolerance_pct", 1.0))
    for apr in liab.aprs:
        bal = apr.balance_subject_to_apr or 0.0
        interest = apr.interest_charge_amount or 0.0
        if bal <= 0 or interest <= 0:
            continue
        # Annualize: (monthly_interest / balance) * 12 * 100 = APR%
        implied = (interest / bal) * 12 * 100
        if implied > apr.apr_percentage + tolerance:
            yield Finding(
                finding_id=_new(rule["rule_id"]),
                account_id=account.account_id,
                kind="discrepancy",
                severity=rule["severity"],
                rule_id=rule["rule_id"],
                title=rule["title"],
                description=(
                    f"{apr.apr_type}: disclosed APR {apr.apr_percentage:.2f}% "
                    f"but the interest charge of ${interest:.2f} on a "
                    f"balance of ${bal:.2f} implies an APR of "
                    f"{implied:.2f}% (tolerance ±{tolerance:.2f}%)."
                ),
                citation=rule["citation"],
                evidence={
                    "disclosed_apr_pct": apr.apr_percentage,
                    "balance_subject_to_apr": bal,
                    "interest_charge_amount": interest,
                    "implied_apr_pct": round(implied, 4),
                    "apr_type": apr.apr_type,
                },
                created_at=now_iso(),
            )


def _check_min_payment(
    account: Account, liab: Liability, rules: dict,
) -> Iterable[Finding]:
    if (liab.minimum_payment_amount is None or
            liab.last_statement_balance is None):
        return
    if liab.minimum_payment_amount > liab.last_statement_balance:
        # No dedicated YAML rule — fall back to the broader CFPB Periodic
        # Statement Rule.
        yield Finding(
            finding_id=_new("card_act.min_payment_exceeds_balance"),
            account_id=account.account_id,
            kind="discrepancy",
            severity="medium",
            rule_id="card_act.min_payment_exceeds_balance",
            title="Minimum payment exceeds statement balance",
            description=(
                f"Minimum payment ${liab.minimum_payment_amount:.2f} is "
                f"greater than the last statement balance "
                f"${liab.last_statement_balance:.2f} — issuer should not "
                f"demand more than the full balance."
            ),
            citation="12 CFR 1026.7(b)",
            evidence={
                "minimum_payment_amount": liab.minimum_payment_amount,
                "last_statement_balance": liab.last_statement_balance,
            },
            created_at=now_iso(),
        )


def _check_over_limit(account: Account, rules: dict) -> Iterable[Finding]:
    rule = rules.get("card_act.over_limit_balance")
    if not rule or account.current_balance is None or not account.credit_limit:
        return
    if account.current_balance > account.credit_limit:
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=account.account_id,
            kind="discrepancy",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"Current balance ${account.current_balance:.2f} exceeds "
                f"credit limit ${account.credit_limit:.2f}."
            ),
            citation=rule["citation"],
            evidence={
                "current_balance": account.current_balance,
                "credit_limit": account.credit_limit,
            },
            created_at=now_iso(),
        )


def _check_late_fees(
    account: Account, txns: list[Transaction], rules: dict,
) -> Iterable[Finding]:
    first_cap = rules["card_act.first_late_fee_cap"]
    repeat_cap = rules["card_act.repeat_late_fee_cap"]
    late_fees = [t for t in txns if LATE_FEE_RE.search(t.name or "")]
    late_fees.sort(key=lambda t: t.posted_date)
    # First in window vs repeats: the CFPB definition is "within six
    # billing cycles" — we approximate as 180 days from the prior fee.
    last_date: date | None = None
    for t in late_fees:
        amount = abs(t.amount)
        is_repeat = (last_date is not None and
                     (t.posted_date - last_date) <= timedelta(days=180))
        cap_rule = repeat_cap if is_repeat else first_cap
        if amount > float(cap_rule["cap_usd"]):
            yield Finding(
                finding_id=_new(cap_rule["rule_id"]),
                account_id=account.account_id,
                kind="discrepancy",
                severity=cap_rule["severity"],
                rule_id=cap_rule["rule_id"],
                title=cap_rule["title"],
                description=(
                    f"A late fee of ${amount:.2f} on {t.posted_date} exceeds "
                    f"the {'repeat' if is_repeat else 'first'}-violation cap "
                    f"of ${cap_rule['cap_usd']:.2f}."
                ),
                citation=cap_rule["citation"],
                evidence={
                    "transaction_id": t.transaction_id,
                    "amount": amount,
                    "cap_usd": float(cap_rule["cap_usd"]),
                    "posted_date": t.posted_date.isoformat(),
                    "is_repeat": is_repeat,
                },
                created_at=now_iso(),
            )
        last_date = t.posted_date


def _check_over_limit_fees(
    account: Account, txns: list[Transaction], rules: dict,
) -> Iterable[Finding]:
    rule = rules.get("card_act.over_limit_fee_requires_opt_in")
    if not rule:
        return
    for t in txns:
        if OVER_LIMIT_FEE_RE.search(t.name or ""):
            yield Finding(
                finding_id=_new(rule["rule_id"]),
                account_id=account.account_id,
                kind="discrepancy",
                severity=rule["severity"],
                rule_id=rule["rule_id"],
                title=rule["title"],
                description=(
                    f"An over-limit fee of ${abs(t.amount):.2f} was posted on "
                    f"{t.posted_date}. Demand proof of affirmative opt-in."
                ),
                citation=rule["citation"],
                evidence={
                    "transaction_id": t.transaction_id,
                    "amount": abs(t.amount),
                    "posted_date": t.posted_date.isoformat(),
                },
                created_at=now_iso(),
            )


def _check_duplicate_charges(
    account: Account, txns: list[Transaction],
) -> Iterable[Finding]:
    """Same merchant + amount within 3 days, non-pending."""
    by_key: dict[tuple, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.pending or t.amount <= 0:
            continue
        key = ((t.merchant_name or t.name).strip().lower(), round(t.amount, 2))
        by_key[key].append(t)
    for (merchant, amount), group in by_key.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda t: t.posted_date)
        for prev, curr in zip(group, group[1:]):
            if (curr.posted_date - prev.posted_date) <= timedelta(days=3):
                yield Finding(
                    finding_id=_new("billing.possible_duplicate_charge"),
                    account_id=account.account_id,
                    kind="discrepancy",
                    severity="low",
                    rule_id="billing.possible_duplicate_charge",
                    title="Possible duplicate charge",
                    description=(
                        f"Two charges of ${amount:.2f} from {merchant!r} "
                        f"posted within 3 days "
                        f"({prev.posted_date} and {curr.posted_date})."
                    ),
                    citation="15 USC 1666 (Fair Credit Billing Act)",
                    evidence={
                        "merchant": merchant,
                        "amount": amount,
                        "transaction_ids": [prev.transaction_id, curr.transaction_id],
                        "dates": [prev.posted_date.isoformat(),
                                  curr.posted_date.isoformat()],
                    },
                    created_at=now_iso(),
                )


# ---- tool registration --------------------------------------------------

def _audit_tool(account_id: str) -> list[dict]:
    return [f.__dict__ for f in audit_account(account_id)]


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="audit_account",
        description="Run the deterministic CARD Act / billing audit on one "
                    "credit card account. Returns a list of Finding dicts "
                    "(discrepancies only — see audit_violations for FDCPA/"
                    "FCRA).",
        parameters_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
            },
            "required": ["account_id"],
        },
        handler=_audit_tool,
    ))
