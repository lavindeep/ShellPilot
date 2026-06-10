"""Interface between the runtime and the terminal UI."""

from __future__ import annotations

from typing import Protocol


class RuntimeUI(Protocol):
    """What the conversation runtime needs from a user interface."""

    def stream_token(self, token: str) -> None: ...

    def show_status(self, text: str) -> None: ...

    def show_error(self, text: str) -> None: ...

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None: ...

    def show_tool_result(self, name: str, success: bool, summary: str) -> None: ...
