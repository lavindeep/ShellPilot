"""Interactive input: two-line prompt with history and slash completion (section 31.2).

PtkInput drives prompt_toolkit for real terminals; PlainInput is the non-TTY
fallback (pipes, tests). Both speak the same InputProvider protocol so the
REPL never cares which one it has.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console

from shellpilot.cli.render import context_line
from shellpilot.cli.theme import Glyphs

PT_STYLE = Style.from_dict(
    {
        "context": "#6b6b6b",
        "chevron": "#98c379 bold",
    }
)


@dataclass(frozen=True)
class PromptContext:
    """What the dim context line shows above the chevron."""

    workspace: Path
    model: str
    profile: str


class InputProvider(Protocol):
    def read(self, context: PromptContext) -> str:
        """Return one stripped input line; EOFError/KeyboardInterrupt pass through."""
        ...


class SlashCompleter(Completer):
    """Completes slash commands, and only when the buffer starts with '/'."""

    def __init__(self, commands: Sequence[str]) -> None:
        self._commands = tuple(commands)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent | None
    ) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for command in self._commands:
            if command.startswith(text) and command != text:
                yield Completion(command, start_position=-len(text))


class PtkInput:
    """prompt_toolkit input: persistent history, tab completion, styled prompt."""

    def __init__(self, history_file: Path, commands: Sequence[str], glyphs: Glyphs) -> None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        self._glyphs = glyphs
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_file)),
            completer=SlashCompleter(commands),
            complete_while_typing=True,
            style=PT_STYLE,
        )

    def read(self, context: PromptContext) -> str:
        ctx = context_line(context.workspace, context.model, context.profile).plain
        prompt = FormattedText(
            [
                ("class:context", ctx + "\n"),
                ("class:chevron", f"{self._glyphs.chevron} "),
            ]
        )
        return self._session.prompt(prompt).strip()


class PlainInput:
    """Non-TTY fallback: prints the context line, reads a plain line."""

    def __init__(
        self,
        console: Console,
        glyphs: Glyphs,
        read_line: Callable[[], str] | None = None,
    ) -> None:
        self._console = console
        self._glyphs = glyphs
        self._read_line = read_line

    def read(self, context: PromptContext) -> str:
        self._console.print(context_line(context.workspace, context.model, context.profile))
        if self._read_line is not None:
            return self._read_line().strip()
        return self._console.input(f"[sp.chevron]{self._glyphs.chevron}[/sp.chevron] ").strip()


def make_input(
    console: Console,
    state_dir: Path,
    commands: Sequence[str],
    glyphs: Glyphs,
) -> InputProvider:
    if console.is_terminal and sys.stdin.isatty():
        return PtkInput(state_dir / "history", commands, glyphs)
    return PlainInput(console, glyphs)
