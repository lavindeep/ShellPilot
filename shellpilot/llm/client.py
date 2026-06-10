"""Provider-neutral LLM client protocol — the seam that makes the runtime testable."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from shellpilot.llm.messages import Message, ToolDefinition
from shellpilot.llm.ollama import LocalModel

TokenCallback = Callable[[str], None]


class LLMClient(Protocol):
    """What the conversation runtime needs from a model provider."""

    def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
        num_ctx: int,
        on_token: TokenCallback | None = None,
    ) -> Message: ...

    def health(self) -> bool: ...

    def list_models(self) -> list[LocalModel]: ...

    def model_context_length(self, model: str) -> int | None: ...
