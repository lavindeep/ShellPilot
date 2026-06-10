"""Unified conversation runtime (design section 10): one loop, no chat/agent split."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.llm.client import LLMClient
from shellpilot.llm.messages import Message, user
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.prompts.system import build_system_prompt
from shellpilot.runtime.budget import ContextBudget, estimate_tokens, resolve_budget
from shellpilot.runtime.events import RuntimeUI

MIN_KEPT_MESSAGES = 4


@dataclass(frozen=True)
class RuntimeStatus:
    """Snapshot for /status and /compact status."""

    model: str
    profile: str
    workspace: Path
    estimated_prompt_tokens: int
    budget: ContextBudget
    history_messages: int


class ConversationRuntime:
    """Owns conversation history, budgets, and model calls for one session."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        settings: Settings,
        workspace: Path,
        behavior: BehaviorInstructions,
        ui: RuntimeUI,
        model: str | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._workspace = workspace
        self._behavior = behavior
        self._ui = ui
        self._model = model or settings.model.default
        self._history: list[Message] = []
        self.budget = self._resolve_budget()

    @property
    def model(self) -> str:
        return self._model

    @property
    def settings(self) -> Settings:
        return self._settings

    def _resolve_budget(self) -> ContextBudget:
        detected = self._llm.model_context_length(self._model)
        return resolve_budget(self._settings.context, detected)

    def set_model(self, model: str) -> None:
        self._model = model
        self.budget = self._resolve_budget()

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings
        self.budget = self._resolve_budget()

    def clear_history(self) -> None:
        self._history.clear()

    def _system_message_text(self) -> str:
        return build_system_prompt(
            workspace=self._workspace,
            profile=self._settings.runtime.security_profile,
            behavior_block=self._behavior.as_prompt_block(),
        )

    def estimated_prompt_tokens(self) -> int:
        total = estimate_tokens(self._system_message_text())
        for message in self._history:
            total += estimate_tokens(message.content)
        return total

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            model=self._model,
            profile=self._settings.runtime.security_profile,
            workspace=self._workspace,
            estimated_prompt_tokens=self.estimated_prompt_tokens(),
            budget=self.budget,
            history_messages=len(self._history),
        )

    def compact_now(self) -> int:
        """Drop oldest turns down to the compaction threshold; returns messages dropped."""
        dropped = 0
        while (
            self.estimated_prompt_tokens() > self.budget.compact_at_tokens
            and len(self._history) > MIN_KEPT_MESSAGES
        ):
            self._history.pop(0)
            dropped += 1
        return dropped

    def run_turn(self, text: str) -> str:
        """One user turn: budget-check, compact, call the model, stream, record."""
        if estimate_tokens(text) > self.budget.max_user_message_tokens:
            self._ui.show_status(
                "Message too large for the model context "
                f"(limit ~{self.budget.max_user_message_tokens} tokens). "
                "Save it to a file and ask me to read it instead."
            )
            return ""

        self._history.append(user(text))
        dropped = self.compact_now()
        if dropped:
            self._ui.show_status(f"Compacted context: dropped {dropped} oldest messages.")

        messages = [Message(role="system", content=self._system_message_text()), *self._history]
        reply = self._llm.chat(
            self._model,
            messages,
            num_ctx=self.budget.model_context_tokens,
            on_token=self._ui.stream_token,
        )
        self._history.append(reply)
        return reply.content
