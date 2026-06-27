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
class ImageRef:
    """A reference to an image attached to a message.

    Stores both the original filesystem path (for display) and the
    base64-encoded bytes (for wire encoding to Ollama).  The sha256 hex
    digest is stored so transcripts can record a content-stable reference
    without embedding the bytes themselves.
    """

    path: str  # original filesystem path (display/reference)
    sha256: str  # hex digest of the raw bytes
    data_b64: str  # base64-encoded raw bytes (for Ollama wire encoding)
    size_bytes: int  # byte length of the raw image (avoids re-decoding b64 to measure)


@dataclass(frozen=True)
class Message:
    """One chat message in provider-neutral form."""

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    images: tuple[ImageRef, ...] = ()
    # Streamed reasoning text from a think-capable model, captured for audit
    # observability only. Never echoed back to the API (see ollama._encode_message);
    # may be surfaced live to the UI when a consumer is wired (see on_thinking).
    # Kept default-empty so existing constructions stand.
    thinking: str = ""
    # Total output tokens for this call, from Ollama's `eval_count`; 0 if absent.
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolDefinition:
    """A tool schema offered to the model (flat JSON schema by design, section 10.4)."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()


def system(content: str) -> Message:
    return Message(role=ROLE_SYSTEM, content=content)


def user(content: str, *, images: tuple[ImageRef, ...] = ()) -> Message:
    return Message(role=ROLE_USER, content=content, images=images)


def assistant(content: str, tool_calls: tuple[ToolCall, ...] = ()) -> Message:
    return Message(role=ROLE_ASSISTANT, content=content, tool_calls=tool_calls)


def tool_result(content: str) -> Message:
    return Message(role=ROLE_TOOL, content=content)
