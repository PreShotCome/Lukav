"""Scan orchestrator. Runs the deterministic audit + the FDCPA/FCRA
flagger, persists the union to the findings table, and returns the rows."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from lukav.models.findings import Finding
from lukav.storage.db import connect as _connect, default_db_path
from lukav.tools.debt_audit import audit_account
from lukav.tools.fdcpa_fcra import audit_violations


FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    finding_id   TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    citation     TEXT NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debt_context (
    account_id   TEXT PRIMARY KEY REFERENCES accounts(account_id) ON DELETE CASCADE,
    payload      TEXT NOT NULL
);
"""


def init_findings(db_path: Optional[Path] = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(FINDINGS_SCHEMA)


def save_context(account_id: str, payload: dict,
                 db_path: Optional[Path] = None) -> None:
    init_findings(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO debt_context (account_id, payload) VALUES (?, ?)
            ON CONFLICT(account_id) DO UPDATE SET payload = excluded.payload
            """,
            (account_id, json.dumps(payload)),
        )


def load_context(account_id: str, db_path: Optional[Path] = None) -> dict:
    init_findings(db_path)
    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT payload FROM debt_context WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return json.loads(r["payload"]) if r else {}


def scan_account(account_id: str, db_path: Optional[Path] = None) -> list[Finding]:
    """Run both audits, persist the result, return the list."""
    init_findings(db_path)
    context = load_context(account_id, db_path=db_path)
    findings = list(audit_account(account_id))
    findings.extend(audit_violations(account_id, context))
    # Wipe prior findings for the account so the table reflects the latest
    # scan (idempotent reruns).
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM findings WHERE account_id = ?", (account_id,))
        for f in findings:
            conn.execute(
                """
                INSERT INTO findings (
                  finding_id, account_id, kind, severity, rule_id, title,
                  description, citation, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f.finding_id, f.account_id, f.kind, f.severity, f.rule_id,
                 f.title, f.description, f.citation,
                 json.dumps(f.evidence, default=str), f.created_at or ""),
            )
    return findings


def list_findings(account_id: str, db_path: Optional[Path] = None) -> list[Finding]:
    init_findings(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM findings WHERE account_id = ? ORDER BY "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "WHEN 'low' THEN 2 ELSE 3 END",
            (account_id,),
        ).fetchall()
    return [
        Finding(
            finding_id=r["finding_id"], account_id=r["account_id"],
            kind=r["kind"], severity=r["severity"], rule_id=r["rule_id"],
            title=r["title"], description=r["description"],
            citation=r["citation"],
            evidence=json.loads(r["evidence"]) if r["evidence"] else {},
            created_at=r["created_at"],
        )
        for r in rows
    ]


def get_finding(finding_id: str, db_path: Optional[Path] = None) -> Optional[Finding]:
    init_findings(db_path)
    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM findings WHERE finding_id = ?", (finding_id,),
        ).fetchone()
    if not r:
        return None
    return Finding(
        finding_id=r["finding_id"], account_id=r["account_id"],
        kind=r["kind"], severity=r["severity"], rule_id=r["rule_id"],
        title=r["title"], description=r["description"],
        citation=r["citation"],
        evidence=json.loads(r["evidence"]) if r["evidence"] else {},
        created_at=r["created_at"],
    )
