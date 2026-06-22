"""Findings — the output of a scan.

A Finding is either a discrepancy (math/fee error) or a violation
(consumer-protection statute red flag). The shape is unified so the UI
and the letter generator can iterate one list."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

Severity = Literal["info", "low", "medium", "high"]
Kind = Literal["discrepancy", "violation"]


@dataclass
class Finding:
    finding_id: str
    account_id: str
    kind: Kind
    severity: Severity
    rule_id: str
    title: str
    description: str
    citation: str               # statute or CFPB ref — required, scan drops findings without one
    evidence: dict = field(default_factory=dict)
    created_at: Optional[str] = None    # ISO 8601 UTC


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
