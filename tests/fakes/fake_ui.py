"""Recording fake for the RuntimeUI protocol."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeUI:
    tokens: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    tool_results: list[tuple[str, bool, str]] = field(default_factory=list)

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
