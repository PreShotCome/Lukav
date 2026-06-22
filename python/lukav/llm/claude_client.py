"""Claude client via the `claude -p` CLI. Lifted from Tech-Support.

Uses Ian's Claude Code subscription auth — no Anthropic API key needed.
Tool-call schema is embedded in the system prompt; Lukav doesn't yet
use tool-calling through Claude (audit + letter generation are
deterministic), so this client is here mostly to support
legal_research's narrative analysis."""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from .base import ChatMessage, LlmClient


def _claude_available() -> bool:
    return shutil.which("claude") is not None


class ClaudeCliClient(LlmClient):
    def __init__(
        self,
        model: Optional[str] = None,
        executable: str = "claude",
        timeout: float = 180.0,
    ) -> None:
        if not _claude_available():
            raise RuntimeError(
                "claude CLI not found on PATH. Install Claude Code first."
            )
        self.model = model
        self.executable = executable
        self.timeout = timeout

    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.2,
    ) -> ChatMessage:
        prompt = _build_prompt(messages)
        cmd = [self.executable, "-p", "--output-format", "text"]
        if self.model:
            cmd.extend(["--model", self.model])
        try:
            result = subprocess.run(
                cmd, input=prompt,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self.timeout, check=False,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"claude CLI not runnable: {e}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s")
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            details = stderr or stdout or "(no output from claude CLI)"
            raise RuntimeError(
                f"claude CLI exit {result.returncode}: {details[:2000]}"
            )
        return ChatMessage(role="assistant", content=(result.stdout or "").strip())


def _build_prompt(messages: list[ChatMessage]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"[system]\n{m.content}\n")
        elif m.role == "user":
            parts.append(f"[user]\n{m.content}\n")
        elif m.role == "assistant":
            parts.append(f"[assistant]\n{m.content}\n")
    parts.append("[assistant]\n")
    return "\n".join(parts)
