"""Recording fake for the RuntimeUI protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalRequest
    from shellpilot.runtime.events import TurnStats
    from shellpilot.runtime.planner import TaskPlan


@dataclass
class FakeUI:
    tokens: list[str] = field(default_factory=list)
    began: int = 0
    ended: int = 0
    turn_stats: list[TurnStats] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    tool_results: list[tuple[str, bool, str]] = field(default_factory=list)
    command_lines: list[str] = field(default_factory=list)
    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    approve_actions: bool = True
    plan_approvals: list[tuple[str, str]] = field(default_factory=list)
    plan_answer: tuple[str, str] = ("y", "")
    plan_progress: list[list[str]] = field(default_factory=list)

    def stream_token(self, token: str) -> None:
        self.tokens.append(token)

    def begin_response(self) -> None:
        self.began += 1

    def end_response(self) -> None:
        self.ended += 1

    def turn_finished(self, stats: TurnStats) -> None:
        self.turn_stats.append(stats)

    def show_status(self, text: str) -> None:
        self.statuses.append(text)

    def show_error(self, text: str) -> None:
        self.errors.append(text)

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        self.tool_calls.append((name, arguments))

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self.tool_results.append((name, success, summary))

    def show_command_output(self, line: str) -> None:
        self.command_lines.append(line)

    def ask_approval(self, request: ApprovalRequest) -> bool:
        self.approval_requests.append(request)
        return self.approve_actions

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        self.plan_approvals.append((plan.task_id, path))
        return self.plan_answer

    def show_plan_progress(self, plan: TaskPlan) -> None:
        self.plan_progress.append([step.status for step in plan.steps])
