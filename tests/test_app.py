"""Tests for the inert full-screen TUI app shell (design section 31, UI v2)."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from shellpilot.cli.app import (
    ASCII_BOX,
    UNICODE_BOX,
    BoxChars,
    _read_git_branch,
    build_app,
    horizontal_border,
)
from shellpilot.cli.slash import command_words
from shellpilot.cli.status_bar import status_bar
from shellpilot.cli.theme import ASCII_GLYPHS, UNICODE_GLYPHS


class StatusBarKwargs(TypedDict):
    workspace: Path
    model: str
    profile: str
    is_cloud: bool
    ctx_pct: int


# --- Pure border-line builder -------------------------------------------------


@pytest.mark.parametrize("width", [4, 5, 20, 80, 200])
def test_horizontal_border_unicode(width: int) -> None:
    top = horizontal_border(width, UNICODE_BOX, top=True)
    bottom = horizontal_border(width, UNICODE_BOX, top=False)
    # Exactly `width` cells, correct rounded corners, solid fill between.
    assert len(top) == width
    assert len(bottom) == width
    assert top[0] == "╭" and top[-1] == "╮"
    assert bottom[0] == "╰" and bottom[-1] == "╯"
    assert top[1:-1] == "─" * (width - 2)
    assert bottom[1:-1] == "─" * (width - 2)


@pytest.mark.parametrize("width", [4, 5, 20, 80, 200])
def test_horizontal_border_ascii(width: int) -> None:
    top = horizontal_border(width, ASCII_BOX, top=True)
    bottom = horizontal_border(width, ASCII_BOX, top=False)
    assert len(top) == width == len(bottom)
    assert top[0] == "+" and top[-1] == "+"
    assert bottom[0] == "+" and bottom[-1] == "+"
    assert set(top[1:-1]) <= {"-"}


def test_horizontal_border_degenerate_widths() -> None:
    # No off-by-one and never longer than the terminal at tiny widths.
    for box in (UNICODE_BOX, ASCII_BOX):
        for width in (0, 1, 2):
            assert len(horizontal_border(width, box, top=True)) == width
    # Width 2 is just the two corners, no fill.
    assert horizontal_border(2, UNICODE_BOX, top=True) == "╭╮"


def test_box_chars_are_single_cells() -> None:
    for box in (UNICODE_BOX, ASCII_BOX):
        assert isinstance(box, BoxChars)
        for ch in (box.top_left, box.top_right, box.horizontal, box.vertical):
            assert len(ch) == 1


# --- status_bar branch segment ------------------------------------------------


def _plain(fragments: StyleAndTextTuples) -> str:
    return "".join(fragment[1] for fragment in fragments)


def test_status_bar_without_branch_is_byte_identical() -> None:
    common = StatusBarKwargs(
        workspace=Path("/tmp/ws"),
        model="gemma4:e4b",
        profile="balanced",
        is_cloud=False,
        ctx_pct=12,
    )
    # The new parameter defaults must not perturb the existing caller's output.
    assert list(status_bar(**common)) == list(status_bar(**common, branch=None))


def test_status_bar_branch_segment_present_and_placed() -> None:
    bar = list(
        status_bar(
            workspace=Path("/tmp/ws"),
            model="gemma4:e4b",
            profile="balanced",
            is_cloud=False,
            ctx_pct=12,
            branch="main",
        )
    )
    text = _plain(bar)
    assert "⎇ main" in text
    # Placed after the profile, before the locality dot.
    assert text.index("balanced") < text.index("⎇ main") < text.index("local")


def test_status_bar_branch_is_sanitized() -> None:
    bar = list(
        status_bar(
            workspace=Path("/tmp/ws"),
            model="m",
            profile="p",
            is_cloud=False,
            ctx_pct=0,
            branch="ma\x1b[31min\x00",
        )
    )
    text = _plain(bar)
    # The control/ANSI bytes are stripped; the de-fanged literal remnant is inert.
    assert "\x1b" not in text and "\x00" not in text
    assert "ma" in text and "in" in text


def test_status_bar_branch_ascii_glyph() -> None:
    bar = list(
        status_bar(
            workspace=Path("/tmp/ws"),
            model="m",
            profile="p",
            is_cloud=False,
            ctx_pct=0,
            branch="main",
            branch_glyph="git:",
        )
    )
    assert "git: main" in _plain(bar)


# --- _read_git_branch ---------------------------------------------------------


def test_read_git_branch_reads_ref(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/feat/ui-app-shell\n", encoding="utf-8")
    assert _read_git_branch(tmp_path) == "feat/ui-app-shell"


def test_read_git_branch_none_for_non_repo(tmp_path: Path) -> None:
    assert _read_git_branch(tmp_path) is None


def test_read_git_branch_none_for_detached_head(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    assert _read_git_branch(tmp_path) is None


def test_read_git_branch_first_line_only(tmp_path: Path) -> None:
    # A crafted HEAD must not inject extra lines into the status-bar dock.
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\nevil\x1b[31m\n", encoding="utf-8")
    assert _read_git_branch(tmp_path) == "main"


def test_read_git_branch_worktree_file_is_none(tmp_path: Path) -> None:
    # A linked worktree has `.git` as a plain file → OSError, fails closed.
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    assert _read_git_branch(tmp_path) is None


# --- Headless app smoke (the anti-garbage proof) ------------------------------


def _build_headless(tmp_path: Path, inp: object, *, unicode: bool = True) -> object:
    return build_app(
        workspace=tmp_path,
        model="gemma4:e4b",
        profile="balanced",
        glyphs=UNICODE_GLYPHS if unicode else ASCII_GLYPHS,
        commands=command_words(),
        input=inp,  # type: ignore[arg-type]
        output=DummyOutput(),
    )


def test_app_constructs_submits_and_exits(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app = _build_headless(tmp_path, inp)
        inp.send_text("hello\n")  # LF → c-j → submit
        inp.send_text("/exit\n")  # quits cleanly
        app.run()  # type: ignore[attr-defined]
        pane = app.layout.get_buffer_by_name("pane")  # type: ignore[attr-defined]
        assert pane is not None
        assert "hello" in pane.text
        # The /exit command quit; it is never echoed into the pane.
        assert "/exit" not in pane.text


def test_app_builds_in_ascii_mode(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app = _build_headless(tmp_path, inp, unicode=False)
        inp.send_text("/exit\n")
        app.run()  # type: ignore[attr-defined]
        # Constructs and exits with the ASCII glyph/box set, no exception.


def test_app_alt_enter_inserts_newline_then_submits(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app = _build_headless(tmp_path, inp)
        # "a", Alt+Enter (ESC + CR) inserts a newline, "b", then Enter submits.
        inp.send_text("a\x1b\rb\r")
        inp.send_text("/exit\n")
        app.run()  # type: ignore[attr-defined]
        pane = app.layout.get_buffer_by_name("pane")  # type: ignore[attr-defined]
        assert pane is not None
        assert "a\nb" in pane.text
