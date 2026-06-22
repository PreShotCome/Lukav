"""Debt-buyer detection — substring-match a collector name against the
curated yaml table."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml


_TABLE_PATH = Path(__file__).resolve().parent / "debt_buyers.yaml"


@dataclass
class BuyerProfile:
    name: str
    aliases: list[str]
    parent: str
    note: str


@lru_cache(maxsize=1)
def _load() -> list[BuyerProfile]:
    with open(_TABLE_PATH) as f:
        data = yaml.safe_load(f) or {}
    out: list[BuyerProfile] = []
    for raw in data.get("buyers", []):
        out.append(BuyerProfile(
            name=raw.get("name", ""),
            aliases=list(raw.get("aliases") or []),
            parent=raw.get("parent") or "",
            note=raw.get("note") or "",
        ))
    return out


def match(collector_name: str) -> Optional[BuyerProfile]:
    if not collector_name:
        return None
    n = collector_name.lower()
    for buyer in _load():
        candidates = [buyer.name.lower(), *(a.lower() for a in buyer.aliases)]
        if any(c and c in n for c in candidates):
            return buyer
    return None


def all_buyers() -> list[BuyerProfile]:
    return list(_load())
