"""Tests for the boot model picker (design section 32)."""

from __future__ import annotations

import io
from collections.abc import Iterator

from rich.console import Console

from shellpilot.cli.model_picker import choose_model, resolve_preselect, should_show_picker
from shellpilot.cli.theme import SHELLPILOT_THEME
from shellpilot.llm.ollama import LocalModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_1_GB = 1_073_741_824


def make_console(answers: list[str]) -> Console:
    console = Console(
        record=True,
        width=100,
        file=io.StringIO(),
        theme=SHELLPILOT_THEME,
        force_terminal=True,
    )
    answer_iter: Iterator[str] = iter(answers)

    def fake_input(prompt: str = "", **kwargs: object) -> str:
        console.print(prompt, end="")  # echo prompt like real Console.input
        return next(answer_iter)

    console.input = fake_input  # type: ignore[method-assign]
    return console


def models(*names: str) -> list[LocalModel]:
    return [LocalModel(name=n, size_bytes=4 * _1_GB) for n in names]


GEMMA = "gemma4:e4b"
QWEN = "qwen3.5:9b-mlx"
LLAMA = "llama4:scout"

THREE_MODELS = models(GEMMA, QWEN, LLAMA)

# ---------------------------------------------------------------------------
# should_show_picker
# ---------------------------------------------------------------------------


def test_should_show_picker_matrix() -> None:
    # Override supplied → skip
    assert should_show_picker(tty=True, model_override="gemma4:e2b", installed_count=3) is False
    # Non-interactive → skip
    assert should_show_picker(tty=False, model_override=None, installed_count=3) is False
    # Only one installed → skip
    assert should_show_picker(tty=True, model_override=None, installed_count=1) is False
    # Zero installed → skip (edge case, same guard)
    assert should_show_picker(tty=True, model_override=None, installed_count=0) is False
    # All conditions met → show
    assert should_show_picker(tty=True, model_override=None, installed_count=2) is True
    assert should_show_picker(tty=True, model_override=None, installed_count=3) is True


# ---------------------------------------------------------------------------
# resolve_preselect
# ---------------------------------------------------------------------------


def test_resolve_preselect_prefers_installed_last_model() -> None:
    installed = {GEMMA, QWEN}
    assert resolve_preselect(GEMMA, QWEN, installed) == QWEN


def test_resolve_preselect_falls_back_when_last_model_gone() -> None:
    installed = {GEMMA, QWEN}
    assert resolve_preselect(GEMMA, "old-model:gone", installed) == GEMMA


def test_resolve_preselect_falls_back_when_last_model_none() -> None:
    installed = {GEMMA, QWEN}
    assert resolve_preselect(GEMMA, None, installed) == GEMMA


# ---------------------------------------------------------------------------
# choose_model
# ---------------------------------------------------------------------------


def test_picker_enter_accepts_preselect() -> None:
    console = make_console([""])
    result = choose_model(console, THREE_MODELS, GEMMA)
    assert result == GEMMA


def test_picker_number_selects_row() -> None:
    console = make_console(["2"])
    result = choose_model(console, THREE_MODELS, GEMMA)
    assert result == QWEN


def test_picker_exact_name_selects() -> None:
    console = make_console([LLAMA])
    result = choose_model(console, THREE_MODELS, GEMMA)
    assert result == LLAMA


def test_picker_invalid_then_valid_reprompts() -> None:
    # "banana" is invalid → reprompt → "2" → second model
    console = make_console(["banana", "2"])
    result = choose_model(console, THREE_MODELS, GEMMA)
    assert result == QWEN
    # The prompt must appear twice in output
    out = console.export_text()
    assert out.count("Select a model") == 2


def test_picker_eof_returns_preselect() -> None:
    console = Console(
        record=True, width=100, file=io.StringIO(), theme=SHELLPILOT_THEME, force_terminal=True
    )

    def raise_eof(prompt: str = "", **kwargs: object) -> str:
        raise EOFError

    console.input = raise_eof  # type: ignore[method-assign]
    result = choose_model(console, THREE_MODELS, GEMMA)
    assert result == GEMMA


def test_picker_marks_untested_models() -> None:
    """Rows for untested families show 'untested'; tested families do not."""
    console = make_console([""])
    choose_model(console, THREE_MODELS, GEMMA)
    out = console.export_text()
    # llama4 is not in TESTED_FAMILIES
    lines = out.splitlines()
    llama_line = next(ln for ln in lines if LLAMA in ln)
    gemma_line = next(ln for ln in lines if GEMMA in ln)
    qwen_line = next(ln for ln in lines if QWEN in ln)
    assert "untested" in llama_line
    assert "untested" not in gemma_line
    assert "untested" not in qwen_line
