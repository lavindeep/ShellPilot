"""Unified conversation runtime (design section 10): one loop, no chat/agent split."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.llm.client import LLMClient
from shellpilot.llm.messages import Message, tool_result, user
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.prompts.system import build_system_prompt
from shellpilot.runtime.budget import ContextBudget, estimate_tokens, resolve_budget
from shellpilot.runtime.events import RuntimeUI
from shellpilot.runtime.executor import ToolExecutor
from shellpilot.tools.registry import ToolRegistry, default_registry

MIN_KEPT_MESSAGES = 4
MAX_CONSECUTIVE_MALFORMED = 2


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
        registry: ToolRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._workspace = workspace
        self._behavior = behavior
        self._ui = ui
        self._model = model or settings.model.default
        self._registry = registry or default_registry()
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
        return self._tool_loop().content

    def _tool_loop(self) -> Message:
        """Model call loop with tool dispatch, budgets, and recovery (section 10.4)."""
        executor = ToolExecutor(
            registry=self._registry,
            workspace=self._workspace,
            profile=self._settings.runtime.security_profile,
            max_result_tokens=self.budget.max_tool_prompt_tokens,
            max_total_tokens=self.budget.max_total_tool_prompt_tokens,
        )
        tools = executor.available_definitions()
        tool_turns = 0
        consecutive_malformed = 0

        while True:
            messages = [
                Message(role="system", content=self._system_message_text()),
                *self._history,
            ]
            reply = self._llm.chat(
                self._model,
                messages,
                tools=tools,
                num_ctx=self.budget.model_context_tokens,
                on_token=self._ui.stream_token,
            )
            self._history.append(reply)
            if not reply.tool_calls:
                return reply

            tool_turns += 1
            if tool_turns > self._settings.runtime.max_tool_turns:
                self._ui.show_status("Tool budget for this turn is exhausted; wrapping up.")
                self._history.append(
                    tool_result(
                        "Tool budget exhausted for this turn. Answer now in plain text "
                        "with what you already know; do not call more tools."
                    )
                )
                tools = []
                continue

            for call in reply.tool_calls:
                self._ui.show_tool_call(call.name, call.arguments)
                outcome = executor.execute(call)
                if outcome.malformed:
                    consecutive_malformed += 1
                    if consecutive_malformed >= MAX_CONSECUTIVE_MALFORMED:
                        self._ui.show_status(
                            "Repeated malformed tool calls; stopping tool use for this turn."
                        )
                        self._history.append(
                            tool_result(
                                f"{outcome.model_text}\nRepeated malformed tool calls. "
                                "Answer now in plain text without calling tools."
                            )
                        )
                        tools = []
                        break
                    self._history.append(
                        tool_result(f"{outcome.model_text}\nRetry once with a corrected call.")
                    )
                    continue
                consecutive_malformed = 0
                if outcome.result is not None:
                    self._ui.show_tool_result(
                        call.name, outcome.result.success, outcome.result.summary
                    )
                self._history.append(tool_result(outcome.model_text))
