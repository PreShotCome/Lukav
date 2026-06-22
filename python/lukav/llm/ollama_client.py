"""Minimal Ollama client (chat completions). Opt-in via LUKAV_LLM_BACKEND=ollama."""
from __future__ import annotations

import os
from typing import Optional

import httpx

from .base import ChatMessage, LlmClient


class OllamaClient(LlmClient):
    def __init__(self, model: Optional[str] = None) -> None:
        self.host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1")

    def chat(
        self,
        messages: list[ChatMessage],
        tools=None,
        temperature: float = 0.2,
    ) -> ChatMessage:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": temperature},
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages if m.role in ("system", "user", "assistant")
            ],
        }
        r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=180.0)
        r.raise_for_status()
        data = r.json()
        return ChatMessage(role="assistant",
                           content=(data.get("message") or {}).get("content", ""))
