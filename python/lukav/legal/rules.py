"""YAML rule pack loader. Each YAML file is a flat list of rule dicts
under top-level `rules:`."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_RULES_DIR = Path(__file__).resolve().parent / "rules"


@lru_cache(maxsize=8)
def load_rules(pack: str) -> dict[str, dict[str, Any]]:
    """Return rule-id → rule-dict mapping for a named pack ('card_act',
    'fdcpa', 'fcra')."""
    path = _RULES_DIR / f"{pack}.yaml"
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {r["rule_id"]: r for r in data.get("rules", [])}


def rule(pack: str, rule_id: str) -> dict[str, Any]:
    rules = load_rules(pack)
    if rule_id not in rules:
        raise KeyError(f"unknown rule {rule_id!r} in pack {pack!r}")
    return rules[rule_id]
