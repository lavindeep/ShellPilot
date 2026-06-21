"""Tests for the interactive input layer (design section 31.2)."""

from __future__ import annotations

import io
from pathlib import Path

from prompt_toolkit.document import Document
from rich.console import Console

from shellpilot.cli.input import (
    PlainInput,
    PromptContext,
    PtkInput,
    SlashCompleter,
    make_input,
)
from shellpilot.cli.slash import command_words
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, build_console
from shellpilot.config.model import Settings

GLYPHS = UNICODE_GLYPHS


def context() -> PromptContext:
    return PromptContext(workspace=Path("/tmp/ws"), model="gemma4:e4b", profile="balanced")


def test_command_words_derives_clean_phrases() -> None:
    words = command_words()
    assert "/help" in words
    assert "/exit" in words and "/quit" not in words  # /quit dropped
    assert "/model use" in words  # argument placeholder stripped
    assert not any("<" in word for word in words)


def test_slash_completer_only_completes_slash_input() -> None:
    completer = SlashCompleter(["/help", "/model list", "/model use"])
    none = list(completer.get_completions(Document("hello"), None))
    assert none == []
    hits = [completion.text for completion in completer.get_completions(Document("/mod"), None)]
    assert "/model list" in hits and "/model use" in hits


def test_plain_input_prints_context_and_reads_line() -> None:
    console = Console(record=True, file=io.StringIO(), theme=SHELLPILOT_THEME)
    provider = PlainInput(console, GLYPHS, read_line=lambda: "  do the thing  ")
    line = provider.read(context())
    assert line == "do the thing"
    out = console.export_text()
    assert "gemma4:e4b · balanced" in out


def test_make_input_falls_back_to_plain_for_non_tty(tmp_path: Path) -> None:
    console = build_console(Settings(), file=io.StringIO())
    provider = make_input(console, tmp_path, command_words(), GLYPHS)
    assert isinstance(provider, PlainInput)


def test_ptk_input_creates_history_location(tmp_path: Path) -> None:
    history = tmp_path / "state" / "history"
    PtkInput(history, ["/help"], GLYPHS)
    assert history.parent.is_dir()
