"""Chat message and tool-call types shared across the LLM layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """One chat message in provider-neutral form."""

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolDefinition:
    """A tool schema offered to the model (flat JSON schema by design, section 10.4)."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()


def system(content: str) -> Message:
    return Message(role=ROLE_SYSTEM, content=content)


def user(content: str) -> Message:
    return Message(role=ROLE_USER, content=content)


def assistant(content: str, tool_calls: tuple[ToolCall, ...] = ()) -> Message:
    return Message(role=ROLE_ASSISTANT, content=content, tool_calls=tool_calls)


def tool_result(content: str) -> Message:
    return Message(role=ROLE_TOOL, content=content)
