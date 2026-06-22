"""OS keyring for Plaid credentials, with an env-var fallback.

Reads / writes in this order:
  1. environment variable (PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV)
  2. OS keyring under service "lukav" — only if `keyring` is installed.

The env-var path keeps tests and CI hermetic; the keyring path keeps
the desktop install secret-store-y."""
from __future__ import annotations

import os
from typing import Optional

SERVICE = "lukav"

try:
    import keyring as _keyring  # type: ignore
except Exception:  # pragma: no cover - optional dep
    _keyring = None


def get_secret(key: str) -> Optional[str]:
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    if _keyring is None:
        return None
    try:
        return _keyring.get_password(SERVICE, key)
    except Exception:
        return None


def set_secret(key: str, value: str) -> None:
    if _keyring is None:
        raise RuntimeError(
            "keyring not installed; install with `pip install lukav[secrets]`"
        )
    _keyring.set_password(SERVICE, key, value)


def have_plaid_creds() -> bool:
    return bool(get_secret("PLAID_CLIENT_ID") and get_secret("PLAID_SECRET"))


def plaid_env() -> str:
    return (get_secret("PLAID_ENV") or "sandbox").lower()
