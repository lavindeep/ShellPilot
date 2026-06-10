"""Recording fake for the RuntimeUI protocol."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeUI:
    tokens: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def stream_token(self, token: str) -> None:
        self.tokens.append(token)

    def show_status(self, text: str) -> None:
        self.statuses.append(text)

    def show_error(self, text: str) -> None:
        self.errors.append(text)
