"""Provider-neutral LLM client protocol — the seam that makes the runtime testable."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from shellpilot.llm.messages import Message, ToolDefinition

if TYPE_CHECKING:
    # Only an annotation (list_models return type); a TYPE_CHECKING import keeps
    # the protocol module free of a runtime dependency on the concrete Ollama
    # client, so ollama.py can import GenerationCancelled from here without an
    # import cycle.
    from shellpilot.llm.ollama import LocalModel

TokenCallback = Callable[[str], None]


class GenerationCancelled(Exception):
    """A model turn was aborted mid-stream via the cancel event (branch 6, §31.15).

    Deliberately NOT an ``OllamaError`` subclass: the think-retry in
    ``OllamaClient.chat`` and the worker in ``TurnRunner`` must distinguish a
    user cancel from a provider failure, so it propagates past both as a clean,
    typed signal rather than a generic error.
    """


class LLMClient(Protocol):
    """What the conversation runtime needs from a model provider."""

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
    ) -> Message: ...

    def health(self) -> bool: ...

    def list_models(self) -> list[LocalModel]: ...

    def model_context_length(self, model: str) -> int | None: ...

    def model_capabilities(self, model: str) -> tuple[str, ...]: ...

    def preload(self, model: str, *, keep_alive: str = "5m") -> None: ...
