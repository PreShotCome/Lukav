"""LLM backend selection.

Resolution order:
  1. explicit `backend` arg (e.g. a --backend flag — none yet)
  2. LUKAV_LLM_BACKEND env var
  3. "claude"

NOTE: Lukav intentionally does NOT honor the bare `LLM_BACKEND` env
var, even though Tech-Support / Theo uses that name. Theo defaults
LLM_BACKEND=ollama on Ian's box (a 7B local model), and silently
inheriting that broke Phase 8 credit-report extraction. Lukav uses its
own namespaced env var so the two apps can't cross-contaminate. Set
LUKAV_LLM_BACKEND=ollama explicitly if you want local inference here.

Set LUKAV_LLM_BACKEND=none (or "off" / "disabled") to skip the LLM
entirely — audit, letters, and Plaid dashboard all still work.
"""
from __future__ import annotations

import os
from typing import Optional

from .base import ChatMessage, LlmClient, ToolCall, ToolResult

__all__ = ["ChatMessage", "LlmClient", "ToolCall", "ToolResult",
           "build_default_client", "describe_default_backend"]


def build_default_client(
    backend: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[LlmClient]:
    backend = (
        backend
        or os.environ.get("LUKAV_LLM_BACKEND")
        or "claude"
    ).lower()
    if backend in ("none", "off", "disabled"):
        return None
    if backend == "claude":
        from .claude_client import ClaudeCliClient, _claude_available
        if not _claude_available():
            return None
        return ClaudeCliClient(
            model=model or os.environ.get("CLAUDE_MODEL") or "claude-opus-4-7"
        )
    if backend == "ollama":
        from .ollama_client import OllamaClient
        return OllamaClient(model=model)
    raise ValueError(f"unknown LUKAV_LLM_BACKEND {backend!r}")


def describe_default_backend() -> dict:
    """For debugging: report which backend would be selected right now
    and why. Powers `python -m lukav --check-llm`."""
    raw = os.environ.get("LUKAV_LLM_BACKEND")
    chosen = (raw or "claude").lower()
    info = {
        "LUKAV_LLM_BACKEND": raw,
        "chosen_backend": chosen,
        "LLM_BACKEND_env_present_but_ignored":
            os.environ.get("LLM_BACKEND"),
    }
    if chosen == "claude":
        from .claude_client import _claude_available
        info["claude_cli_on_path"] = _claude_available()
        info["resolved"] = "claude" if _claude_available() else "none (no claude CLI)"
    elif chosen == "ollama":
        info["resolved"] = "ollama"
        info["OLLAMA_MODEL"] = os.environ.get("OLLAMA_MODEL")
        info["OLLAMA_HOST"] = os.environ.get("OLLAMA_HOST")
    elif chosen in ("none", "off", "disabled"):
        info["resolved"] = "none (explicitly disabled)"
    else:
        info["resolved"] = f"unknown ({chosen})"
    return info
