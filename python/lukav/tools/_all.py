"""Single source of truth for registering every Lukav tool module.

Same pattern as Tech-Support/python/agent/tools/_all.py. Phase 1+ adds
plaid_tools, debt_audit, fdcpa_fcra, legal_research, dispute_letter."""
from __future__ import annotations

from .base import ToolRegistry


def build_full_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for _, module in _import_modules():
        module.register(registry)
    return registry


def _import_modules():
    from . import plaid_tools, debt_audit, fdcpa_fcra
    return [
        ("plaid", plaid_tools),
        ("debt_audit", debt_audit),
        ("fdcpa_fcra", fdcpa_fcra),
    ]
