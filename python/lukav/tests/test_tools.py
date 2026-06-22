"""Tool registry tests."""
from __future__ import annotations

from lukav.tools._all import build_full_registry


def test_registry_includes_phase1_tools():
    registry = build_full_registry()
    names = registry.names()
    assert "list_linked_cards" in names
    assert "sync_item" in names
    assert "get_account_snapshot" in names


def test_tool_schemas_are_well_formed():
    registry = build_full_registry()
    for schema in registry.schemas():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"]
        assert fn["description"]
        assert "parameters" in fn


def test_list_linked_cards_runs_against_empty_db():
    registry = build_full_registry()
    tool = registry.get("list_linked_cards")
    assert tool is not None
    out = tool.call({})
    assert out.strip() in ("[]", "")
