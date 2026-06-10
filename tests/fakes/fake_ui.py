"""Recording fake for the RuntimeUI protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalRequest


@dataclass
class FakeUI:
    tokens: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    tool_results: list[tuple[str, bool, str]] = field(default_factory=list)
    command_lines: list[str] = field(default_factory=list)
    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    approve_actions: bool = True
    plan_approvals: list[tuple[str, str]] = field(default_factory=list)
    plan_answer: tuple[str, str] = ("y", "")

    def stream_token(self, token: str) -> None:
        self.tokens.append(token)

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

    def ask_plan_approval(self, rendered: str, path: str) -> tuple[str, str]:
        self.plan_approvals.append((rendered, path))
        return self.plan_answer
