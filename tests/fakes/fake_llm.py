"""Scripted fake LLM client (design section 26.3).

The fake makes the whole runtime testable in CI without a GPU or Ollama: it
emits direct answers, tool calls, malformed tool calls, and stuck loops from a
fixed script, and records every request it receives.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from shellpilot.llm.client import GenerationCancelled, TokenCallback
from shellpilot.llm.messages import Message, ToolCall, ToolDefinition, assistant
from shellpilot.llm.ollama import LocalModel

DEFAULT_CONTEXT_LENGTH = 8192


def answer(text: str) -> Message:
    """Script entry: a plain assistant answer."""
    return assistant(text)


def tool_call(name: str, **arguments: Any) -> Message:
    """Script entry: a single well-formed tool call."""
    return assistant("", tool_calls=(ToolCall(name=name, arguments=arguments),))


@dataclass
class RecordedCall:
    model: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...]
    num_ctx: int
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeLLM:
    """Plays back a script of Messages; raises if asked for more than scripted."""

    script: list[Message] = field(default_factory=list)
    models: list[LocalModel] = field(
        default_factory=lambda: [LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000)]
    )
    context_length: int = DEFAULT_CONTEXT_LENGTH
    healthy: bool = True
    calls: list[RecordedCall] = field(default_factory=list)
    preloads: list[tuple[str, str]] = field(default_factory=list)
    capabilities: tuple[str, ...] = ("completion", "tools", "vision")

    def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
        num_ctx: int,
        options: dict[str, Any] | None = None,
        on_token: TokenCallback | None = None,
        on_thinking: TokenCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Message:
        # Branch 6: honor a set cancel event the same way OllamaClient does —
        # abort before consuming the script, so a cancelled turn never produces a
        # (partial) reply. A None/unset event leaves every existing test unchanged.
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled
        self.calls.append(
            RecordedCall(
                model=model,
                messages=tuple(messages),
                tools=tuple(tools),
                num_ctx=num_ctx,
                options=dict(options or {}),
            )
        )
        if not self.script:
            raise AssertionError("FakeLLM script exhausted: unexpected extra chat() call")
        reply = self.script.pop(0)
        if on_token is not None and reply.content:
            for chunk in _chunks(reply.content, 8):
                on_token(chunk)
        if on_thinking is not None and reply.thinking:
            for chunk in _chunks(reply.thinking, 8):
                on_thinking(chunk)
        return reply

    def health(self) -> bool:
        return self.healthy

    def list_models(self) -> list[LocalModel]:
        return list(self.models)

    def model_context_length(self, model: str) -> int | None:
        return self.context_length

    def model_capabilities(self, model: str) -> tuple[str, ...]:
        return self.capabilities

    def preload(self, model: str, *, keep_alive: str = "5m") -> None:
        self.preloads.append((model, keep_alive))


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
