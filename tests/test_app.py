"""Tests for the inert full-screen TUI app shell (design section 31, UI v2)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples, to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from shellpilot.cli.app import (
    ASCII_BOX,
    UNICODE_BOX,
    BoxChars,
    StatusValues,
    _branch_resolver,
    _read_git_branch,
    _scroll_down,
    _scroll_up,
    build_app,
    horizontal_border,
)
from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.slash import command_words
from shellpilot.cli.status_bar import status_bar
from shellpilot.cli.theme import ASCII_GLYPHS, UNICODE_GLYPHS
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel


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


def test_scroll_up_from_follow_leaves_bottom() -> None:
    # Following (None) → cursor sits at last_line; PageUp moves up one page and
    # pins a concrete line (leaves follow mode).
    assert _scroll_up(None, last_line=20, page=8) == 12


def test_scroll_up_clamps_to_top() -> None:
    assert _scroll_up(3, last_line=20, page=8) == 0


def test_scroll_down_moves_toward_bottom() -> None:
    assert _scroll_down(2, last_line=20, page=8) == 10


def test_scroll_down_resumes_follow_at_bottom() -> None:
    # Paging down to (or past) the last line returns None → auto-follow resumes.
    assert _scroll_down(18, last_line=20, page=8) is None


def test_scroll_down_while_following_stays_following() -> None:
    assert _scroll_down(None, last_line=20, page=8) is None


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


# --- _branch_resolver (status-bar branch tracks /cwd) -------------------------


def _repo(path: Path, branch: str) -> Path:
    git = path / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return path


def test_branch_resolver_rereads_on_workspace_change(tmp_path: Path) -> None:
    # The status bar's branch segment must follow /cwd: into a repo with a
    # different branch, and out of a repo entirely (→ None).
    repo_a = _repo(tmp_path / "a", "main")
    repo_b = _repo(tmp_path / "b", "feature")
    plain = tmp_path / "plain"
    plain.mkdir()

    resolve = _branch_resolver(repo_a, "main")
    assert resolve(repo_a) == "main"  # seeded build-time value
    assert resolve(repo_b) == "feature"  # /cwd into another repo → re-read
    assert resolve(plain) is None  # /cwd out of any repo → no branch
    assert resolve(repo_a) == "main"  # /cwd back → re-read


def test_branch_resolver_does_not_reread_unchanged_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The .git/HEAD read happens ONLY on a workspace change, never per render —
    # the perf reason the branch was build-time in the first place.
    calls: list[Path] = []

    def _spy(ws: Path) -> str | None:
        calls.append(ws)
        return "spied"

    monkeypatch.setattr("shellpilot.cli.app._read_git_branch", _spy)
    resolve = _branch_resolver(tmp_path, "seed")
    assert resolve(tmp_path) == "seed"  # same workspace → seeded value, no read
    assert resolve(tmp_path) == "seed"
    assert calls == []  # never re-read while the workspace is unchanged
    other = tmp_path / "other"
    assert resolve(other) == "spied"  # changed → exactly one read
    assert calls == [other]


# --- Headless app smoke (the anti-garbage proof) ------------------------------


def _build_headless(
    tmp_path: Path,
    inp: object,
    *,
    unicode: bool = True,
) -> tuple[Application[None], AppUI]:
    glyphs = UNICODE_GLYPHS if unicode else ASCII_GLYPHS
    ui = AppUI(glyphs=glyphs, width_fn=lambda: 80)
    app = build_app(
        workspace=tmp_path,
        model="gemma4:e4b",
        profile="balanced",
        glyphs=glyphs,
        commands=command_words(),
        input=inp,  # type: ignore[arg-type]
        output=DummyOutput(),
        ui=ui,
    )
    return app, ui


def test_app_constructs_submits_and_exits(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app, ui = _build_headless(tmp_path, inp)
        inp.send_text("hello\n")  # LF → c-j → submit
        inp.send_text("/exit\n")  # quits cleanly
        app.run()
        ansi = ui._render_ansi()
        assert "hello" in ansi
        # The /exit command quit; it is never echoed into the pane.
        assert "/exit" not in ansi


def test_app_builds_in_ascii_mode(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app, _ = _build_headless(tmp_path, inp, unicode=False)
        inp.send_text("/exit\n")
        app.run()
        # Constructs and exits with the ASCII glyph/box set, no exception.


def test_app_alt_enter_inserts_newline_then_submits(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        app, ui = _build_headless(tmp_path, inp)
        # "a", Alt+Enter (ESC + CR) inserts a newline, "b", then Enter submits.
        inp.send_text("a\x1b\rb\r")
        inp.send_text("/exit\n")
        app.run()
        from rich.text import Text as RichText

        # The multi-line dock text "a\nb" is echoed via show_status as a single
        # Text renderable; check its .plain property for the preserved newline.
        texts = [r.plain for r in ui._renderables if isinstance(r, RichText)]
        assert any("a\nb" in t for t in texts)
        assert not any("/exit" in t for t in texts)


# --- Branch-6 Ctrl-C on_interrupt wiring (§31.15) -----------------------------


def _run_with_interrupt(tmp_path: Path, *, on_interrupt: object) -> AppUI:
    """Build a headless app with on_interrupt wired, press Ctrl-C, then /exit."""
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_interrupt=on_interrupt,  # type: ignore[arg-type]
        )
        inp.send_text("\x03")  # Ctrl-C (ETX) — a key press, not a SIGINT
        inp.send_text("/exit\n")
        app.run()
    return ui


def test_c_c_calls_on_interrupt_and_skips_idle_hint_when_true(tmp_path: Path) -> None:
    calls: list[int] = []

    def on_interrupt() -> bool:
        calls.append(1)
        return True  # a turn was cancelled

    ui = _run_with_interrupt(tmp_path, on_interrupt=on_interrupt)
    assert calls == [1]
    # A cancelled turn shows its own marker (from abort_turn), NOT the idle hint.
    assert "idle" not in ui._render_ansi()


def test_c_c_shows_idle_hint_when_on_interrupt_returns_false(tmp_path: Path) -> None:
    calls: list[int] = []

    def on_interrupt() -> bool:
        calls.append(1)
        return False  # nothing in flight

    ui = _run_with_interrupt(tmp_path, on_interrupt=on_interrupt)
    assert calls == [1]
    assert "idle" in ui._render_ansi()


def test_c_c_shows_idle_hint_when_on_interrupt_none(tmp_path: Path) -> None:
    # Back-compat: with no on_interrupt (the inert shell), Ctrl-C shows the hint.
    ui = _run_with_interrupt(tmp_path, on_interrupt=None)
    assert "idle" in ui._render_ansi()


# --- Branch-7 approval focus-swap routing (§31.16) ----------------------------


class _FakeGate:
    """Records the keybinding handshake without the real Future plumbing.

    One ``submit``/``cancel`` resolves the fake prompt (``active`` flips False),
    mirroring the real gate clearing ``_pending`` once the future resolves.
    """

    def __init__(
        self,
        *,
        active: bool = True,
        dock_risk: RiskLevel | None = None,
        dock_hint: str | None = None,
    ) -> None:
        self._active = active
        self.submitted: list[str] = []
        self.cancelled = 0
        self.dock_risk = dock_risk if active else None
        self.dock_hint = dock_hint if active else None

    @property
    def active(self) -> bool:
        return self._active

    def submit(self, line: str) -> None:
        self.submitted.append(line)
        self._active = False
        self.dock_risk = None
        self.dock_hint = None

    def cancel(self) -> None:
        self.cancelled += 1
        self._active = False
        self.dock_risk = None
        self.dock_hint = None


def _build_with_gate(
    tmp_path: Path,
    inp: object,
    gate: _FakeGate,
    *,
    on_interrupt: object | None = None,
) -> tuple[Application[None], AppUI]:
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    app = build_app(
        workspace=tmp_path,
        model="gemma4:e4b",
        profile="balanced",
        glyphs=UNICODE_GLYPHS,
        commands=command_words(),
        input=inp,  # type: ignore[arg-type]
        output=DummyOutput(),
        ui=ui,
        on_interrupt=on_interrupt,  # type: ignore[arg-type]
        approval_gate=gate,  # type: ignore[arg-type]
    )
    return app, ui


def test_approval_active_submit_routes_to_gate(tmp_path: Path) -> None:
    gate = _FakeGate(active=True)
    with create_pipe_input() as inp:
        app, ui = _build_with_gate(tmp_path, inp, gate)
        inp.send_text("y\n")  # gate active → gate.submit("y"), gate deactivates
        inp.send_text("/exit\n")  # gate now inactive → real /exit quits
        app.run()
    assert gate.submitted == ["y"]
    assert gate.cancelled == 0
    # The routed answer did NOT fall through to the inert show_status echo path
    # (that path runs only when no gate intercepts the submit).
    assert ui._renderables == []


def test_approval_active_intercepts_exit(tmp_path: Path) -> None:
    # A mid-approval "/exit" is an approval answer (gate.submit), NOT a quit — the
    # gate check sits BEFORE the /exit check.
    gate = _FakeGate(active=True)
    with create_pipe_input() as inp:
        app, ui = _build_with_gate(tmp_path, inp, gate)
        inp.send_text("/exit\n")  # routed to the gate, deactivates it
        inp.send_text("/exit\n")  # now inactive → quits
        app.run()
    assert gate.submitted == ["/exit"]
    assert "/exit" not in ui._render_ansi()


def test_approval_active_ctrl_c_cancels_gate_not_interrupt(tmp_path: Path) -> None:
    gate = _FakeGate(active=True)
    interrupts: list[int] = []

    def on_interrupt() -> bool:
        interrupts.append(1)
        return True

    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate, on_interrupt=on_interrupt)
        inp.send_text("\x03")  # Ctrl-C while gate active → gate.cancel()
        inp.send_text("/exit\n")  # gate now inactive → quits
        app.run()
    assert gate.cancelled == 1
    assert interrupts == []  # model cancel did NOT fire during the approval


def test_ctrl_c_falls_through_to_interrupt_when_gate_inactive(tmp_path: Path) -> None:
    # Existing behavior preserved: with no active approval, Ctrl-C reaches
    # on_interrupt (the turn-level cancel), not the gate.
    gate = _FakeGate(active=False)
    interrupts: list[int] = []

    def on_interrupt() -> bool:
        interrupts.append(1)
        return True

    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate, on_interrupt=on_interrupt)
        inp.send_text("\x03")
        inp.send_text("/exit\n")
        app.run()
    assert interrupts == [1]
    assert gate.cancelled == 0


def test_eof_cancels_active_gate(tmp_path: Path) -> None:
    gate = _FakeGate(active=True)
    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate)
        inp.send_text("\x04")  # Ctrl-D (EOF) while gate active → gate.cancel()
        inp.send_text("/exit\n")  # gate now inactive → quits
        app.run()
    assert gate.cancelled == 1


# --- Branch-8 slash/manual-shell routing (§31.17) -----------------------------


def test_slash_and_bang_lines_route_to_on_slash(tmp_path: Path) -> None:
    # A typed slash or `!` line goes to the router (on_slash), NOT the model turn
    # (on_submit); a normal line still goes to on_submit; /exit still quits.
    submits: list[str] = []
    slashes: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            on_slash=slashes.append,
        )
        inp.send_text("/help\n")  # → on_slash
        inp.send_text("!ls\n")  # → on_slash
        inp.send_text("just talk\n")  # → on_submit (model turn)
        inp.send_text("/exit\n")  # → quits, never reaches on_slash
        app.run()
    assert slashes == ["/help", "!ls"]
    assert submits == ["just talk"]


def test_slash_without_on_slash_falls_through_to_on_submit(tmp_path: Path) -> None:
    # Back-compat: with no on_slash wired (the inert fallback), a slash line still
    # reaches on_submit exactly as before branch 8.
    submits: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
        )
        inp.send_text("/help\n")
        inp.send_text("/exit\n")
        app.run()
    assert submits == ["/help"]


# --- Branch-9 input-dock polish (§31.18) --------------------------------------


def _find_control_text(app: Application[None], needle: str) -> str:
    """Rendered text of the first FormattedTextControl whose line contains needle.

    Calls the control's text callable directly (under ``set_app`` so the borders
    can read the size), bypassing the render-counter fragment cache that never
    advances without a live render loop. Returns ``""`` when no control matches.
    """
    with set_app(app):
        for control in app.layout.find_all_controls():
            text = getattr(control, "text", None)
            if not callable(text):
                continue
            try:
                fragments = to_formatted_text(text())
            except Exception:  # noqa: BLE001 - a non-fragment control just won't match
                continue
            rendered = "".join(fragment[1] for fragment in fragments)
            if needle in rendered:
                return rendered
    return ""


def _chip_visible(app: Application[None]) -> bool:
    """Whether the queued-message chip is shown (its ConditionalContainer filter)."""
    for container in app.layout.walk():
        if isinstance(container, ConditionalContainer):
            return bool(container.filter())
    raise AssertionError("chip ConditionalContainer not found")


def _pane_control(app: Application[None]) -> FormattedTextControl:
    for control in app.layout.find_all_controls():
        if type(control).__name__ == "_PaneControl":
            assert isinstance(control, FormattedTextControl)
            return control
    raise AssertionError("pane control not found")


def _scroll_event(event_type: MouseEventType) -> MouseEvent:
    return MouseEvent(
        position=Point(0, 0),
        event_type=event_type,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )


def test_queue_stages_while_busy_and_fires_at_idle(tmp_path: Path) -> None:
    # A submit while a turn is in flight is staged (chip shown), not dispatched;
    # the registered idle callback fires it once the turn ends (§31.18).
    busy = {"on": True}
    idle: list[Callable[[], None]] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            is_busy=lambda: busy["on"],
            register_idle=idle.append,
        )
        inp.send_text("hello\n")  # busy → staged, NOT submitted
        inp.send_text("/exit\n")  # /exit quits even while busy
        app.run()
    assert submits == []  # staged, never dispatched
    assert _chip_visible(app) is True
    assert "queued: hello" in _find_control_text(app, "queued")
    assert len(idle) == 1  # build_app registered exactly one idle callback
    # Turn ends (busy clears); the idle callback fires the staged line as a turn.
    busy["on"] = False
    idle[0]()
    assert submits == ["hello"]
    assert _chip_visible(app) is False  # slot drained


def test_queue_one_slot_replaces_prior(tmp_path: Path) -> None:
    # A second submit while busy replaces the first — one slot, latest wins.
    idle: list[Callable[[], None]] = []
    submits: list[str] = []
    busy = {"on": True}
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            is_busy=lambda: busy["on"],
            register_idle=idle.append,
        )
        inp.send_text("first\n")
        inp.send_text("second\n")
        inp.send_text("/exit\n")
        app.run()
    assert "queued: second" in _find_control_text(app, "queued")
    busy["on"] = False
    idle[0]()
    assert submits == ["second"]  # only the latest, never both


def test_up_arrow_recalls_staged_message(tmp_path: Path) -> None:
    # is_busy is True only for the FIRST submit, so the recalled line (submitted
    # next) dispatches — proving Up pulled the staged text back into the dock and
    # cleared the slot (§31.18).
    calls = {"n": 0}

    def is_busy() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    submits: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            is_busy=is_busy,
        )
        inp.send_text("recall me\n")  # staged (busy #1)
        inp.send_text("\x1b[A")  # Up in an empty dock → recall into the box
        inp.send_text("\n")  # submit the recalled line (busy #2 False) → dispatch
        inp.send_text("/exit\n")
        app.run()
    assert submits == ["recall me"]
    assert _chip_visible(app) is False  # recall cleared the slot


def test_up_arrow_passthrough_when_nothing_staged(tmp_path: Path) -> None:
    # With nothing staged the filter is false, so Up is the default (cursor/history)
    # and never wipes the in-progress line; the typed text submits intact.
    submits: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            is_busy=lambda: False,
        )
        inp.send_text("hello")  # no newline → stays in the box
        inp.send_text("\x1b[A")  # Up: nothing staged → NOT recall
        inp.send_text("\n")  # submit "hello" intact
        inp.send_text("/exit\n")
        app.run()
    assert submits == ["hello"]


def test_up_arrow_passthrough_when_box_nonempty(tmp_path: Path) -> None:
    # With a message staged BUT text in the box the filter is false: Up does not
    # recall (the staged slot survives) and the typed line submits as itself.
    calls = {"n": 0}

    def is_busy() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    submits: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
            is_busy=is_busy,
        )
        inp.send_text("staged\n")  # busy #1 → staged
        inp.send_text("typed")  # box now non-empty
        inp.send_text("\x1b[A")  # Up: box non-empty → NOT recall
        inp.send_text("\n")  # submit "typed" (busy #2 False) → dispatch
        inp.send_text("/exit\n")
        app.run()
    assert submits == ["typed"]  # the typed line, NOT the staged "staged"
    assert _chip_visible(app) is True  # the staged slot is untouched


def test_tab_completes_cwd_set_path_argument(tmp_path: Path) -> None:
    (tmp_path / "Projects").mkdir()
    slashes: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_slash=slashes.append,
        )
        inp.send_text("/cwd set Pro")
        inp.send_text("\t")
        inp.send_text("\n")
        inp.send_text("/exit\n")
        app.run()
    assert slashes == ["/cwd set Projects/"]


def test_tab_completes_path_at_cursor_and_preserves_suffix(tmp_path: Path) -> None:
    (tmp_path / "Projects").mkdir()
    slashes: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_slash=slashes.append,
        )
        inp.send_text("/cwd set Pro suffix")
        inp.send_text("\x1b[D" * len(" suffix"))
        inp.send_text("\t")
        inp.send_text("\n")
        inp.send_text("/exit\n")
        app.run()
    assert slashes == ["/cwd set Projects/ suffix"]


def test_down_arrow_selects_next_path_completion(tmp_path: Path) -> None:
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "Alpine").mkdir()
    slashes: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_slash=slashes.append,
        )
        inp.send_text("/cwd set Al")
        inp.send_text("\x1b[B")
        inp.send_text("\t")
        inp.send_text("\n")
        inp.send_text("/exit\n")
        app.run()
    assert slashes == ["/cwd set Alpine/"]


def test_tab_completion_escapes_spaces_for_dispatcher(tmp_path: Path) -> None:
    (tmp_path / "My Project").mkdir()
    slashes: list[str] = []
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_slash=slashes.append,
        )
        inp.send_text("/cwd set My")
        inp.send_text("\t")
        inp.send_text("\n")
        inp.send_text("/exit\n")
        app.run()
    assert slashes == ["/cwd set My\\ Project/"]


def test_mouse_wheel_scroll_pins_then_resumes_follow(tmp_path: Path) -> None:
    # Mouse-wheel scroll drives the SAME cursor-line model as PageUp/PageDown:
    # SCROLL_UP pins a line (leaves follow), SCROLL_DOWN back to bottom resumes it.
    with create_pipe_input() as inp:
        app, ui = _build_headless(tmp_path, inp)
        for i in range(30):
            ui.show_status(f"line {i}")
        pane = _pane_control(app)
        last = pane.get_cursor_position().y
        assert pane.mouse_handler(_scroll_event(MouseEventType.SCROLL_UP)) is None
        assert pane.get_cursor_position().y < last  # pinned above the bottom
        assert pane.mouse_handler(_scroll_event(MouseEventType.SCROLL_DOWN)) is None
        assert pane.get_cursor_position().y == last  # back to bottom → follow


def test_mouse_non_scroll_event_delegates_to_super(tmp_path: Path) -> None:
    # A non-wheel mouse event is handled by the base control (returns NotImplemented),
    # so clicks still reach prompt_toolkit's default handling.
    with create_pipe_input() as inp:
        app, _ = _build_headless(tmp_path, inp)
        pane = _pane_control(app)
        assert pane.mouse_handler(_scroll_event(MouseEventType.MOUSE_UP)) is NotImplemented


def test_status_fn_reflects_live_values(tmp_path: Path) -> None:
    # The status bar re-reads status_fn per render, so /model use (cloud!),
    # /profile use, /cwd set, and context growth reflect immediately (§31.18).
    state = {"model": "gemma4:e4b", "cloud": False, "ctx": 5}

    def status_fn() -> StatusValues:
        return StatusValues(
            workspace=tmp_path,
            model=state["model"],
            profile="balanced",
            is_cloud=bool(state["cloud"]),
            ctx_pct=int(state["ctx"]),
        )

    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 120)
        app = build_app(
            workspace=tmp_path,
            model="STATIC",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            status_fn=status_fn,
        )
        before = _find_control_text(app, " ctx")
        assert "gemma4:e4b" in before
        assert "● local" in before
        assert "STATIC" not in before  # status_fn overrides the build-time model
        # A mid-session switch to a cloud model + context growth.
        state["model"] = "gemma4:31b-cloud"
        state["cloud"] = True
        state["ctx"] = 88
        after = _find_control_text(app, " ctx")
    assert "gemma4:31b-cloud" in after
    assert "☁ CLOUD" in after  # the live, unspoofable cloud indicator
    assert "88%" in after


def test_status_fn_none_uses_static_values(tmp_path: Path) -> None:
    # Back-compat: with no status_fn the bar shows the build-time params (the
    # standalone shell + existing callers stay byte-identical).
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 120)
        app = build_app(
            workspace=tmp_path,
            model="static-model",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            is_cloud=False,
            ctx_pct=7,
        )
        text = _find_control_text(app, " ctx")
    assert "static-model" in text
    assert "7%" in text
    assert "● local" in text


def test_ascii_chip_uses_ascii_marker(tmp_path: Path) -> None:
    # The queued chip degrades to an ASCII marker in ASCII mode (no unicode leak).
    with create_pipe_input() as inp:
        ui = AppUI(glyphs=ASCII_GLYPHS, width_fn=lambda: 80)
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=ASCII_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            is_busy=lambda: True,
            register_idle=lambda _cb: None,
        )
        inp.send_text("hello\n")  # busy → staged
        inp.send_text("/exit\n")
        app.run()
    chip = _find_control_text(app, "queued")
    # ASCII mode: the "queued:" label IS the marker; no unicode glyph leaks.
    assert "queued:" in chip
    assert "hello" in chip
    assert "⏳" not in chip


# --- Diff/trail collapse toggle: click + Ctrl-O fallback (§31.16) -------------


def _long_diff(n: int = 30) -> str:
    # A unified diff adding n lines → n diff rows; > WINDOW_ROWS (24) so it
    # overflows the collapse cap and is therefore toggleable.
    body = "".join(f"+line{i}\n" for i in range(n))
    return f"--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,{n} @@\n{body}"


def _seed_diff_ui() -> AppUI:
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    ui.show_approval(
        ApprovalRequest(
            kind="command",
            display="patch x.py",
            risk=RiskLevel.HIGH,
            reasons=("edits a file",),
            cwd=Path("/tmp/ws"),  # display-only, never touched
            diff=_long_diff(),
        )
    )
    return ui


# Ctrl-O = ASCII 0x0F (SI); the pipe-input parser maps it to Keys.ControlO.
_CTRL_O = "\x0f"


def test_ctrl_o_toggles_latest_diff(tmp_path: Path) -> None:
    # Ctrl-O with a diff present toggles the latest diff's collapse state — the
    # keyboard fallback for terminals without mouse reporting (§31.16). A modifier
    # key, so it never collides with typing.
    ui = _seed_diff_ui()
    assert ui._latest_diff is not None and ui._latest_diff.expanded is False
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
        )
        inp.send_text(_CTRL_O)  # diff present → toggle
        inp.send_text("/exit\n")
        app.run()
    assert ui._latest_diff.expanded is True


def test_ctrl_o_pressed_twice_returns_to_collapsed(tmp_path: Path) -> None:
    ui = _seed_diff_ui()
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
        )
        inp.send_text(_CTRL_O + _CTRL_O)  # toggle, then toggle back
        inp.send_text("/exit\n")
        app.run()
    assert ui._latest_diff is not None and ui._latest_diff.expanded is False


def test_letter_o_types_literally_with_diff(tmp_path: Path) -> None:
    # Regression: the fallback is on Ctrl-O, NOT the bare letter, so a message that
    # starts with 'o' types literally even with a diff on screen — pressing 'o'
    # never swallows the character or toggles the diff.
    submits: list[str] = []
    ui = _seed_diff_ui()
    assert ui._latest_diff is not None and ui._latest_diff.expanded is False
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            on_submit=submits.append,
        )
        inp.send_text("open it\n")  # starts with 'o' — must type literally, not toggle
        inp.send_text("/exit\n")
        app.run()
    assert submits == ["open it"]
    assert ui._latest_diff.expanded is False  # never toggled


def test_ctrl_o_during_approval_does_not_toggle(tmp_path: Path) -> None:
    # During an active approval the dock IS the approval input; the toggle filter is
    # false, so Ctrl-O does not toggle the diff and the approval is unaffected.
    ui = _seed_diff_ui()
    assert ui._latest_diff is not None and ui._latest_diff.expanded is False
    gate = _FakeGate(active=True)
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
            approval_gate=gate,  # type: ignore[arg-type]
        )
        inp.send_text(_CTRL_O)  # approval active → filter false → no toggle
        inp.send_text("y\n")  # resolve the approval to deactivate the gate
        inp.send_text("/exit\n")  # gate inactive → quits
        app.run()
    assert ui._latest_diff.expanded is False  # never toggled
    assert gate.submitted == ["y"]


def _click_event(line: int) -> MouseEvent:
    return MouseEvent(
        position=Point(0, line),
        event_type=MouseEventType.MOUSE_UP,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )


def test_click_on_diff_line_toggles_it(tmp_path: Path) -> None:
    # A pane click (MOUSE_UP) on a line inside a diff's transcript range toggles its
    # collapse state and is reported handled (returns None) — the primary, per-element
    # toggle that reaches any diff regardless of age (§31.16).
    ui = _seed_diff_ui()
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
        )
        ui._render_ansi()  # populate the line index
        start = next(s for s, _e, el in ui._toggle_ranges if el is ui._latest_diff)
        pane = _pane_control(app)
        assert pane.mouse_handler(_click_event(start)) is None  # handled
    assert ui._latest_diff is not None and ui._latest_diff.expanded is True


def test_click_outside_any_diff_delegates_to_super(tmp_path: Path) -> None:
    # A click on a line that is not part of any diff/trail is NOT swallowed: the
    # handler returns NotImplemented so default mouse handling still applies.
    ui = _seed_diff_ui()
    with create_pipe_input() as inp:
        app = build_app(
            workspace=tmp_path,
            model="gemma4:e4b",
            profile="balanced",
            glyphs=UNICODE_GLYPHS,
            commands=command_words(),
            input=inp,  # type: ignore[arg-type]
            output=DummyOutput(),
            ui=ui,
        )
        ui._render_ansi()
        end = max(e for _s, e, _el in ui._toggle_ranges)
        pane = _pane_control(app)
        assert pane.mouse_handler(_click_event(end + 5)) is NotImplemented
    assert ui._latest_diff is not None and ui._latest_diff.expanded is False  # untouched


# --- In-app slash menu (§31.20) ----------------------------------------------


def _set_dock_text(app: Application[None], text: str) -> None:
    buffer = app.layout.get_buffer_by_name("dock")
    assert buffer is not None
    buffer.text = text


def _menu_app(
    tmp_path: Path,
    inp: object,
    slashes: list[str],
    submits: list[str],
) -> Application[None]:
    return build_app(
        workspace=tmp_path,
        model="gemma4:e4b",
        profile="balanced",
        glyphs=UNICODE_GLYPHS,
        commands=command_words(),
        input=inp,  # type: ignore[arg-type]
        output=DummyOutput(),
        ui=AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80),
        on_slash=slashes.append,
        on_submit=submits.append,
    )


def test_slash_menu_renders_filtered_rows(tmp_path: Path) -> None:
    # Typing "/co" shows only commands whose phrase starts with it; the menu
    # control renders those rows (§31.20).
    with create_pipe_input() as inp:
        app, _ = _build_headless(tmp_path, inp)
        _set_dock_text(app, "/co")
        rows = _find_control_text(app, "/config show")  # first /co match in the window
        assert "/config show" in rows
        assert "/status" not in rows  # filtered out by the /co prefix


def test_slash_menu_shows_at_most_three_rows(tmp_path: Path) -> None:
    # A bare "/" matches every command but the window shows exactly 3 rows.
    with create_pipe_input() as inp:
        app, _ = _build_headless(tmp_path, inp)
        _set_dock_text(app, "/")
        rows = _find_control_text(app, "/help")
        assert rows.count("\n") == 2  # 3 rows → 2 separators


def test_slash_menu_hidden_without_leading_slash(tmp_path: Path) -> None:
    # No leading slash → no menu rows render at all.
    with create_pipe_input() as inp:
        app, _ = _build_headless(tmp_path, inp)
        _set_dock_text(app, "hello")
        assert _find_control_text(app, "/help") == ""


def test_slash_menu_enter_runs_argless_command(tmp_path: Path) -> None:
    # Smart Enter (\r) on an argless command runs it immediately.
    slashes: list[str] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        app = _menu_app(tmp_path, inp, slashes, submits)
        inp.send_text("/status\r")  # menu open, argless → runs
        inp.send_text("/exit\r")
        app.run()
    assert slashes == ["/status"]


def test_slash_menu_down_arrow_and_enter_fills_arg_command(tmp_path: Path) -> None:
    # Down-arrow navigates to /model use (3rd match); smart Enter FILLS it (does
    # not run an argless command), so the typed arg completes a real line.
    slashes: list[str] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        app = _menu_app(tmp_path, inp, slashes, submits)
        inp.send_text("/model")  # matches /model, /model list, /model use
        inp.send_text("\x1b[B\x1b[B")  # down, down → /model use (takes args)
        inp.send_text("\r")  # smart Enter: fills "/model use ", no run
        inp.send_text("llama\r")  # continue the arg, then submit
        inp.send_text("/exit\r")
        app.run()
    assert slashes == ["/model use llama"]


def test_slash_menu_arrow_selection_previews_command_in_dock(tmp_path: Path) -> None:
    # Arrow navigation writes the highlighted command into the dock immediately
    # while keeping the menu open, so users can continue through sibling matches.
    slashes: list[str] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        app = _menu_app(tmp_path, inp, slashes, submits)
        inp.send_text("/model")  # matches /model, /model list, /model use
        inp.send_text("\x1b[B\x1b[B")  # down, down previews /model use
        inp.send_text(" llama\r")  # continue from the previewed command
        inp.send_text("/exit\r")
        app.run()
    assert slashes == ["/model use llama"]


def test_slash_menu_tab_fills_without_running(tmp_path: Path) -> None:
    # Tab fills the highlighted command + a space and never runs it; a later Enter
    # (menu now closed by the space) submits the filled line.
    slashes: list[str] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        app = _menu_app(tmp_path, inp, slashes, submits)
        inp.send_text("/stat\t")  # Tab fills "/status "
        inp.send_text("\r")  # menu closed → submit
        inp.send_text("/exit\r")
        app.run()
    assert slashes == ["/status "]


def test_bare_message_bypasses_menu_and_submits(tmp_path: Path) -> None:
    # A message with no leading slash never engages the menu — it submits normally.
    slashes: list[str] = []
    submits: list[str] = []
    with create_pipe_input() as inp:
        app = _menu_app(tmp_path, inp, slashes, submits)
        inp.send_text("hello\r")
        inp.send_text("/exit\r")
        app.run()
    assert submits == ["hello"]
    assert slashes == []


# --- Modal dock (the border reflects the approval state) ----------------------


def test_horizontal_border_embeds_label() -> None:
    top = horizontal_border(24, UNICODE_BOX, top=True, label="approve?")
    assert len(top) == 24
    assert top.startswith("╭─ approve? ─")
    assert top.endswith("╮")
    bottom = horizontal_border(24, UNICODE_BOX, top=False, label="approve?")
    assert bottom[1:-1] == "─" * 22  # label rides the TOP border only


def test_horizontal_border_label_ascii() -> None:
    top = horizontal_border(24, ASCII_BOX, top=True, label="approve?")
    assert len(top) == 24
    assert top.startswith("+- approve? -")
    assert top.endswith("+")


def test_horizontal_border_label_dropped_when_it_cannot_fit() -> None:
    for width in (0, 2, 8):
        line = horizontal_border(width, UNICODE_BOX, top=True, label="a long label")
        assert len(line) == width
        assert "a long" not in line


def _border_lines(app: Application[None]) -> list[str]:
    """Rendered text of the two dock border lines (top first)."""
    lines: list[str] = []
    with set_app(app):
        for control in app.layout.find_all_controls():
            text = getattr(control, "text", None)
            if not callable(text):
                continue
            try:
                fragments = to_formatted_text(text())
            except Exception:  # noqa: BLE001 - a non-fragment control won't match
                continue
            rendered = "".join(fragment[1] for fragment in fragments)
            if (
                rendered
                and set(rendered[1:-1]) <= {"─", " "} | set("abcdefghijklmnopqrstuvwxyz\"'?")
                and rendered[0] in "╭╰"
            ):
                lines.append(rendered)
    return lines


def _border_styles(app: Application[None]) -> set[str]:
    """Style strings carried by the dock border fragments."""
    styles: set[str] = set()
    with set_app(app):
        for control in app.layout.find_all_controls():
            text = getattr(control, "text", None)
            if not callable(text):
                continue
            try:
                fragments = to_formatted_text(text())
            except Exception:  # noqa: BLE001
                continue
            for style, content, *_ in fragments:
                if content and content[0] in "╭╰":
                    styles.add(style)
    return styles


def test_dock_border_idle_is_faint_with_no_label(tmp_path: Path) -> None:
    gate = _FakeGate(active=False)
    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate)
        tops = [ln for ln in _border_lines(app) if ln.startswith("╭")]
        assert tops and all(set(ln[1:-1]) == {"─"} for ln in tops)
        assert all("#444444" in s for s in _border_styles(app))
        inp.send_text("/exit\n")
        app.run()


def test_dock_border_high_approval_is_red_and_labeled(tmp_path: Path) -> None:
    gate = _FakeGate(active=True, dock_risk=RiskLevel.HIGH, dock_hint='type "run" to execute')
    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate)
        tops = [ln for ln in _border_lines(app) if ln.startswith("╭")]
        assert tops and any('type "run" to execute' in ln for ln in tops)
        assert all("#e06c75" in s for s in _border_styles(app))
        inp.send_text("y\n")  # resolve the fake gate
        inp.send_text("/exit\n")
        app.run()


def test_dock_border_medium_approval_is_amber(tmp_path: Path) -> None:
    gate = _FakeGate(active=True, dock_risk=RiskLevel.MEDIUM, dock_hint="approve?")
    with create_pipe_input() as inp:
        app, _ = _build_with_gate(tmp_path, inp, gate)
        tops = [ln for ln in _border_lines(app) if ln.startswith("╭")]
        assert tops and any("approve?" in ln for ln in tops)
        assert all("#e5c07b" in s for s in _border_styles(app))
        inp.send_text("y\n")
        inp.send_text("/exit\n")
        app.run()
