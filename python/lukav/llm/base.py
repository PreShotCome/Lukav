"""LLM client abstraction. Lifted from Tech-Support/python/agent/llm/base.py."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: Optional[str] = None
    tool_calls: list["ToolCall"] = field(default_factory=list)
    tool_call_id: Optional[str] = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str


class LlmClient(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.2,
    ) -> ChatMessage:
        """Send a conversation, get the next assistant message back."""
