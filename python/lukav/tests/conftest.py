"""Shared fixtures. Every test that touches storage gets an isolated
SQLite DB and clean Plaid creds env, so order independence is preserved."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "lukav.db"
    monkeypatch.setenv("LUKAV_DB", str(db_file))
    # Strip Plaid env so tests don't accidentally talk to real Plaid.
    for k in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"):
        monkeypatch.delenv(k, raising=False)
    from lukav.storage import db
    db.init_db()
    yield db_file
