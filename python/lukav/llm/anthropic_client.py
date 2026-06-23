"""Anthropic API client — direct SDK, no CLI in the loop.

Use when:
  - the `claude` CLI auth keeps expiring (401 from `claude -p`)
  - you want stable batch processing for credit-report ingest
  - you're running headless and don't have Claude Code installed

Activate by setting LUKAV_LLM_BACKEND=anthropic and providing
ANTHROPIC_API_KEY. Optional ANTHROPIC_MODEL pin (default:
claude-haiku-4-5 — cheap and fast for the JSON-extraction work this
app does)."""
from __future__ import annotations

import os
from typing import Optional

from .base import ChatMessage, LlmClient


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _api_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY") or None


class AnthropicClient(LlmClient):
    def __init__(self, model: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: float = 180.0) -> None:
        if not _anthropic_available():
            raise RuntimeError(
                "anthropic SDK not installed. Install with "
                "`pip install lukav[anthropic]`."
            )
        key = api_key or _api_key()
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set it as an env var (or in "
                "the OS keyring under service 'lukav') and retry."
            )
        import anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout)
        self.model = model or os.environ.get("ANTHROPIC_MODEL") \
            or "claude-haiku-4-5"

    def chat(
        self,
        messages: list[ChatMessage],
        tools=None,
        temperature: float = 0.2,
    ) -> ChatMessage:
        # Anthropic API splits system out of the messages list.
        system_parts = [m.content for m in messages if m.role == "system"]
        user_assistant = [m for m in messages if m.role in ("user", "assistant")]
        # Coerce empty conversation to a no-op user turn so the API
        # accepts it.
        if not user_assistant:
            user_assistant = [ChatMessage(role="user", content="")]
        api_messages = [
            {"role": m.role, "content": m.content} for m in user_assistant
        ]
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system="\n\n".join(system_parts) if system_parts else None,
            messages=api_messages,
            temperature=temperature,
        )
        # Concatenate text blocks.
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        return ChatMessage(role="assistant", content=text)
