"""Tool broker: validation, dispatch, bounding, and malformed-call recovery.

Recovery loops are designed behavior, not error handling (design section 10.4):
a malformed call gets a compact schema reminder and exactly one retry; the same
failure twice stops tool use for the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shellpilot.llm.messages import ToolCall, ToolDefinition
from shellpilot.runtime.budget import estimate_tokens, truncate_to_tokens
from shellpilot.tools.base import (
    ToolContext,
    ToolError,
    ToolResult,
    schema_reminder,
    validate_args,
)
from shellpilot.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ExecutionOutcome:
    """What one tool call produced, in model-facing form."""

    model_text: str
    malformed: bool
    result: ToolResult | None


class ToolExecutor:
    """Executes tool calls under profile, schema, and budget constraints."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        workspace: Path,
        profile: str,
        max_result_tokens: int,
        max_total_tokens: int,
    ) -> None:
        self._registry = registry
        self._workspace = workspace
        self._profile = profile
        self._max_result_tokens = max_result_tokens
        self._max_total_tokens = max_total_tokens
        self._spent_tokens = 0

    def available_definitions(self) -> list[ToolDefinition]:
        return self._registry.definitions_for_profile(self._profile)

    def execute(self, call: ToolCall) -> ExecutionOutcome:
        spec = self._registry.get(call.name)
        if spec is None or self._profile not in spec.allowed_profiles:
            names = ", ".join(d.name for d in self.available_definitions())
            return ExecutionOutcome(
                model_text=f"error: unknown tool '{call.name}'. Available tools: {names}.",
                malformed=True,
                result=None,
            )
        error = validate_args(spec, call.arguments)
        if error is not None:
            return ExecutionOutcome(
                model_text=f"error: {error}. Schema: {schema_reminder(spec)}",
                malformed=True,
                result=None,
            )

        context = ToolContext(workspace=self._workspace, max_result_tokens=self._max_result_tokens)
        try:
            result = spec.handler(context, call.arguments)
        except ToolError as exc:
            result = ToolResult(success=False, summary=str(exc), content="")
        except Exception as exc:  # noqa: BLE001 - tool crashes must not kill the loop
            result = ToolResult(
                success=False, summary=f"tool {call.name} crashed: {exc}", content=""
            )
        return ExecutionOutcome(
            model_text=self._render(call.name, result), malformed=False, result=result
        )

    def _render(self, name: str, result: ToolResult) -> str:
        status = "ok" if result.success else "failed"
        header = f"tool: {name}\nstatus: {status}\nsummary: {result.summary}"
        content = result.content
        if content:
            remaining = self._max_total_tokens - self._spent_tokens
            if remaining <= 0:
                content = "[omitted: total tool-output budget for this turn is spent]"
            else:
                content, _ = truncate_to_tokens(content, min(remaining, self._max_result_tokens))
        text = f"{header}\n---\n{content}" if content else header
        if result.truncated:
            text += "\n(note: output truncated)"
        self._spent_tokens += estimate_tokens(text)
        return text
