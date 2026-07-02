"""Tool broker: validation, dispatch, bounding, and malformed-call recovery.

Recovery loops are designed behavior, not error handling (design section 10.4):
a malformed call gets a compact schema reminder and exactly one retry; the same
failure twice stops tool use for the turn.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolCall, ToolDefinition
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.policy.approvals import (
    DECLINE,
    ApprovalReply,
    ApprovalRequest,
    Decision,
    decide,
)
from shellpilot.policy.explanations import explain_risk
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.budget import estimate_tokens, truncate_to_tokens
from shellpilot.tools.base import (
    ToolContext,
    ToolError,
    ToolResult,
    schema_reminder,
    validate_args,
    workspace_display,
)
from shellpilot.tools.registry import ToolRegistry

ApprovalAsker = Callable[[ApprovalRequest], ApprovalReply]


@dataclass(frozen=True)
class ExecutionOutcome:
    """What one tool call produced, in model-facing form."""

    model_text: str
    malformed: bool
    result: ToolResult | None
    stop_turn: bool = False


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
        max_capture_chars: int = 200_000,
        command_timeout_seconds: int = 600,
        ask_approval: ApprovalAsker | None = None,
        emit_output: Callable[[str], None] | None = None,
        snapshots: SnapshotStore | None = None,
        audit: AuditLogger | None = None,
        allow_sensitive_reads: str = "ask",
        cancel: threading.Event | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._audit = audit
        self._registry = registry
        self._workspace = workspace
        self._profile = profile
        self._max_result_tokens = max_result_tokens
        self._max_total_tokens = max_total_tokens
        self._max_capture_chars = max_capture_chars
        self._command_timeout_seconds = command_timeout_seconds
        self._ask_approval = ask_approval
        self._emit_output = emit_output
        self._allow_sensitive_reads = allow_sensitive_reads
        self._cancel = cancel
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

        context = ToolContext(
            workspace=self._workspace,
            max_result_tokens=self._max_result_tokens,
            max_capture_chars=self._max_capture_chars,
            command_timeout_seconds=self._command_timeout_seconds,
            emit_output=self._emit_output,
            snapshots=self._snapshots,
            allow_sensitive_reads=self._allow_sensitive_reads,
            cancel=self._cancel,
        )

        if spec.precheck is not None:
            precheck_msg = spec.precheck(context, call.arguments)
            if precheck_msg is not None:
                self._log_precheck(call, precheck_msg)
                result = ToolResult(success=False, summary=precheck_msg, content="")
                return ExecutionOutcome(
                    model_text=self._render(call.name, result),
                    malformed=False,
                    result=result,
                )

        # Deterministic policy before execution (sections 14.1-14.3). The model
        # never downgrades this classification (section 14.4).
        classification = spec.risk_for(context, call.arguments)
        decision = decide(
            self._profile,
            spec.side_effect,
            classification.risk,
            self._allow_sensitive_reads,
        )
        display = self._display_for(call)
        if decision is Decision.BLOCK:
            reason = "; ".join(classification.reasons) or "blocked by policy"
            self._log("policy_block", call, display, classification.risk, reason=reason)
            return ExecutionOutcome(
                model_text=(
                    f"tool: {call.name}\nstatus: blocked\nsummary: {reason}. "
                    "Do not retry this action."
                ),
                malformed=False,
                result=ToolResult(success=False, summary=f"blocked: {reason}", content=""),
            )
        if decision is Decision.ASK:
            diff = ""
            if spec.preview is not None:
                try:
                    diff = spec.preview(context, call.arguments)
                except Exception as exc:  # noqa: BLE001 - preview must never block approval
                    diff = f"(preview failed: {exc})"
            purpose = ""
            if classification.risk is RiskLevel.HIGH and spec.side_effect is not SideEffect.NONE:
                # Deterministic purpose explanation for dangerous commands
                # (section 13.4): built from the classifier reasons by a pure
                # function, with no model call. It can never downgrade the
                # deterministic risk classification. Skipped for NONE-side-effect
                # tools: a HIGH-risk sensitive read gets the standard prompt with
                # the classifier reason and no purpose at all (design section 15).
                purpose = explain_risk(classification.reasons)
            request = ApprovalRequest(
                kind="command" if call.name == "run_command" else "tool",
                display=display,
                risk=classification.risk,
                reasons=classification.reasons,
                cwd=self._workspace,
                purpose=purpose,
                diff=diff,
            )
            reply = self._ask_approval(request) if self._ask_approval else DECLINE
            steer = reply.steer_text if not reply.approved else None
            self._log(
                "approval",
                call,
                display,
                classification.risk,
                explanation=purpose,
                decision="approved" if reply.approved else ("steered" if steer else "rejected"),
            )
            if not reply.approved:
                # Reject-and-steer: the un-approved action NEVER runs (design
                # section 14.6). On a plain decline we tell the model not to
                # retry; on a steer we feed the user's guidance back so the
                # model re-proposes a corrected action, which re-enters this
                # same classify->decide->gate flow as any tool call.
                if steer:
                    model_text = (
                        f"tool: {call.name}\nstatus: declined\nsummary: the user declined "
                        f"this action and asks you to do this instead: {steer}. "
                        "Propose a corrected action."
                    )
                    summary = "steered by user"
                else:
                    model_text = (
                        f"tool: {call.name}\nstatus: declined\nsummary: the user declined "
                        "this action. Do not retry it; ask the user how to proceed if needed."
                    )
                    summary = "declined by user"
                return ExecutionOutcome(
                    model_text=model_text,
                    malformed=False,
                    result=ToolResult(success=False, summary=summary, content=""),
                    stop_turn=not bool(steer),
                )

        # Egress visibility (F12): a NETWORK-side-effect tool sends a query/url
        # off-box regardless of model locality. Record it here — after every
        # gate has passed, immediately before the call actually leaves the box —
        # so a blocked/declined call (which never ran) produces no egress
        # record. The AuditLogger redacts the args (e.g. a secret in a URL).
        if spec.side_effect is SideEffect.NETWORK and self._audit is not None:
            self._audit.write("web_egress", tool=call.name, args=dict(call.arguments))

        try:
            result = spec.handler(context, call.arguments)
        except ToolError as exc:
            result = ToolResult(success=False, summary=str(exc), content="")
        except Exception as exc:  # noqa: BLE001 - tool crashes must not kill the loop
            result = ToolResult(
                success=False, summary=f"tool {call.name} crashed: {exc}", content=""
            )
        event = "command_result" if call.name == "run_command" else "tool_result"
        if call.name in ("write_file", "patch_file") and result.success:
            event = "file_edit"
        self._log(
            event,
            call,
            display,
            classification.risk,
            success=result.success,
            summary=result.summary,
        )
        return ExecutionOutcome(
            model_text=self._render(call.name, result), malformed=False, result=result
        )

    def _log_precheck(self, call: ToolCall, message: str) -> None:
        if self._audit is not None:
            display = self._display_for(call)
            self._audit.write(
                "precheck_failed",
                tool=call.name,
                command=display,
                risk=RiskLevel.LOW.value,
                message=message,
            )

    def _log(
        self, event: str, call: ToolCall, display: str, risk: RiskLevel, **fields: Any
    ) -> None:
        if self._audit is not None:
            self._audit.write(event, tool=call.name, command=display, risk=risk.value, **fields)

    def _display_for(self, call: ToolCall) -> str:
        if call.name == "run_command":
            argv = call.arguments.get("argv")
            if isinstance(argv, list):
                return " ".join(str(token) for token in argv)
        rendered = ", ".join(
            f"{key}={self._display_value(key, value)}" for key, value in call.arguments.items()
        )
        return f"{call.name}({rendered})"

    def _display_value(self, key: str, value: Any) -> str:
        # Display-integrity (design section 14.5): a `path` argument is shown as
        # the resolved, workspace-relative target — the SAME resolution the
        # handler acts on — so the approval display can never diverge from the
        # file actually touched. All other args render verbatim.
        if key == "path" and isinstance(value, str):
            return repr(workspace_display(self._workspace, value))
        return repr(value)

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
