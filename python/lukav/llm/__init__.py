"""LLM backend selection. Lifted from Tech-Support/python/agent/llm/.

Order:
  1. explicit `backend` arg
  2. LUKAV_LLM_BACKEND env var (falls back to LLM_BACKEND for parity
     with Theo)
  3. 'claude' — Lukav defaults to Claude because legal reasoning
     benefits from the strongest model. Set LUKAV_LLM_BACKEND=ollama to
     swap. Setting to 'none' disables LLM (audit and letters still work
     without it).
"""
from __future__ import annotations

import os
from typing import Optional

from .base import ChatMessage, LlmClient, ToolCall, ToolResult

__all__ = ["ChatMessage", "LlmClient", "ToolCall", "ToolResult",
           "build_default_client"]


def build_default_client(
    backend: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[LlmClient]:
    backend = (
        backend
        or os.environ.get("LUKAV_LLM_BACKEND")
        or os.environ.get("LLM_BACKEND")
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
    raise ValueError(f"unknown LLM_BACKEND {backend!r}")
