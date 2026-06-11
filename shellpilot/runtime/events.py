"""Interface between the runtime and the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalRequest


@dataclass(frozen=True)
class TurnStats:
    """Post-turn stats for the UI (design section 31.8)."""

    elapsed_s: float
    context_tokens: int
    context_pct: int
    warn: bool


class RuntimeUI(Protocol):
    """What the conversation runtime needs from a user interface."""

    def stream_token(self, token: str) -> None: ...

    def begin_response(self) -> None:
        """The runtime is about to call the model (start a waiting indicator)."""
        ...

    def end_response(self) -> None:
        """The model call finished or failed (always called; stop indicators)."""
        ...

    def turn_finished(self, stats: TurnStats) -> None: ...

    def show_status(self, text: str) -> None: ...

    def show_error(self, text: str) -> None: ...

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None: ...

    def show_tool_result(self, name: str, success: bool, summary: str) -> None: ...

    def show_command_output(self, line: str) -> None: ...

    def ask_approval(self, request: ApprovalRequest) -> bool: ...

    def ask_plan_approval(self, rendered: str, path: str) -> tuple[str, str]:
        """Returns (choice, revision_text); choice is 'y', 'e', or 'n'."""
        ...
