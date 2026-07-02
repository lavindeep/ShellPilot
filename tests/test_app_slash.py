"""Tests for slash/manual-shell routing in the full-screen app (§31.17, branch 8).

The threading + run_in_terminal glue is validated live by the orchestrator; here
the router's effects are INJECTED (no prompt_toolkit needed), so every routing
decision and the pane-capture path are exercised deterministically.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from shellpilot.cli.app_slash import SlashRouter
from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.manual_shell import run_manual_command_captured
from shellpilot.cli.slash import SlashAction, needs_background, needs_terminal, needs_worker
from shellpilot.cli.theme import SHELLPILOT_THEME

# ---------------------------------------------------------------------------
# needs_terminal — exhaustive TRUE cases + representative FALSE cases
# ---------------------------------------------------------------------------

# Every slash form that confirms / prompts for consent / prints to its own
# stdout / preloads. Mirrors the self._confirm + cloud-consent + run_doctor
# call sites in slash.py (see the module NOTE in app_slash classification).
_TERMINAL_LINES = [
    "/shell",
    "/clear",
    "/doctor",
    "/plan cancel",
    "/cwd set /tmp",
    "/config set model.default gemma4:e4b",
    "/config reset",
    "/memory add be concise",
    "/memory forget m-1",
    "/memory compact",
    "/model use gemma4:e4b",
]

_LOOP_LINES = [
    "/help",
    "/status",
    "/plan",
    "/plan path",
    "/cwd",
    "/config show",
    "/config unset model.default",
    "/config reload",
    "/config edit",
    "/model",
    "/model list",
    "/memory show",
    "/compact",
    "/compact status",
    "",
    "   ",
    "hello",
]


def test_needs_terminal_true_cases() -> None:
    for line in _TERMINAL_LINES:
        assert needs_terminal(line) is True, line


def test_needs_terminal_false_cases() -> None:
    for line in _LOOP_LINES:
        assert needs_terminal(line) is False, line


def test_needs_terminal_is_case_insensitive() -> None:
    assert needs_terminal("/CLEAR") is True
    assert needs_terminal("/MODEL USE gemma4:e4b") is True
    assert needs_terminal("/Config Set k v") is True
    assert needs_terminal("/HELP") is False


def test_needs_terminal_tolerates_extra_whitespace() -> None:
    assert needs_terminal("   /clear   ") is True
    assert needs_terminal("  /plan   cancel ") is True
    assert needs_terminal("  /help ") is False


# ---------------------------------------------------------------------------
# SlashRouter — injected effects, no prompt_toolkit
# ---------------------------------------------------------------------------


class FakeDispatch:
    """Records (line, console) calls; prints a marker to the given console."""

    def __init__(
        self, action: SlashAction = SlashAction.CONTINUE, output: str = "DISPATCHED"
    ) -> None:
        self.action = action
        self.output = output
        self.calls: list[tuple[str, Console]] = []

    def __call__(self, line: str, console: Console) -> SlashAction:
        self.calls.append((line, console))
        console.print(self.output)
        return self.action


class FakeTerminal:
    """Records run_in_terminal fns; runs them inline when ``run`` is True."""

    def __init__(self, run: bool = True) -> None:
        self.run = run
        self.fns: list[object] = []

    def __call__(self, fn: object) -> None:
        self.fns.append(fn)
        if self.run:
            fn()  # type: ignore[operator]


class FakeWorker:
    """Records start_action fns; runs them inline when ``run`` is True. Returns
    True (started) like the real ``TurnRunner.start_action``."""

    def __init__(self, run: bool = True) -> None:
        self.run = run
        self.fns: list[object] = []

    def __call__(self, fn: object) -> bool:
        self.fns.append(fn)
        if self.run:
            fn()  # type: ignore[operator]
        return True


class FakeShell:
    """Records `!<cmd>` runs; returns a canned (exit_code, output)."""

    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.commands: list[str] = []

    def __call__(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        return self.exit_code, self.output


def make_router(
    *,
    dispatch: FakeDispatch | None = None,
    terminal: FakeTerminal | None = None,
    worker: FakeWorker | None = None,
    shell: FakeShell | None = None,
    is_busy: bool = False,
    workspace_fn: Callable[[], Path] | None = None,
) -> tuple[SlashRouter, AppUI, FakeDispatch, FakeTerminal, list[str], list[int], io.StringIO]:
    ui = AppUI(width_fn=lambda: 80)
    dispatch = dispatch or FakeDispatch()
    terminal = terminal or FakeTerminal()
    worker = worker or FakeWorker()
    shell = shell or FakeShell()
    manual_lines: list[str] = []
    exits: list[int] = []
    real_buf = io.StringIO()
    real_console = Console(file=real_buf, force_terminal=True, theme=SHELLPILOT_THEME, width=80)
    router = SlashRouter(
        ui=ui,
        dispatch=dispatch,
        real_console=real_console,
        width_fn=lambda: 80,
        run_terminal=terminal,
        run_worker=worker,
        schedule=lambda fn: fn(),  # marshal runs inline in tests
        manual_shell=manual_lines.append,
        run_shell=shell,
        on_exit=lambda: exits.append(1),
        is_busy=lambda: is_busy,
        workspace_fn=workspace_fn,
    )
    return router, ui, dispatch, terminal, manual_lines, exits, real_buf


def test_fast_command_captured_into_pane() -> None:
    router, ui, dispatch, terminal, _, _, _ = make_router(dispatch=FakeDispatch(output="HELLO-OUT"))
    router.route("/help")
    # Dispatched once, against a CAPTURING console (not the real one).
    assert len(dispatch.calls) == 1
    line, console = dispatch.calls[0]
    assert line == "/help"
    # Output captured and pushed to the pane.
    assert "HELLO-OUT" in ui._render_ansi()
    # The fast path never touches run_in_terminal.
    assert terminal.fns == []


def test_interactive_command_routes_to_terminal_real_console() -> None:
    dispatch = FakeDispatch(output="TERM-OUT")
    router, ui, _, terminal, _, _, real_buf = make_router(dispatch=dispatch)
    router.route("/clear")  # needs_terminal → run_in_terminal
    assert len(terminal.fns) == 1  # scheduled under run_in_terminal
    # The fn ran (FakeTerminal.run=True) and dispatched against the REAL console.
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0][1] is router._real_console
    # Output went to the real terminal, NOT loop-captured into the pane.
    assert "TERM-OUT" in real_buf.getvalue()
    assert "TERM-OUT" not in ui._render_ansi()


def test_invalid_cwd_set_is_reported_in_pane_without_terminal_handoff() -> None:
    dispatch = FakeDispatch(output="missing is not a directory.")
    router, ui, _, terminal, _, _, real_buf = make_router(dispatch=dispatch)

    router.route("/cwd set /definitely/not/here")

    assert terminal.fns == []
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0][1] is not router._real_console
    assert "missing is not a directory." in ui._render_ansi()
    assert "missing is not a directory." not in real_buf.getvalue()


def test_valid_cwd_set_still_routes_to_terminal_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "next"
    target.mkdir()
    dispatch = FakeDispatch(output="Workspace boundary changed.")
    router, ui, _, terminal, _, _, real_buf = make_router(
        dispatch=dispatch,
        workspace_fn=lambda: tmp_path,
    )

    router.route("/cwd set next")

    assert len(terminal.fns) == 1
    assert dispatch.calls[0][1] is router._real_console
    assert "Workspace boundary changed." in real_buf.getvalue()
    assert "Workspace boundary changed." not in ui._render_ansi()


def test_valid_cwd_set_with_escaped_space_routes_to_terminal_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "My Project"
    target.mkdir()
    dispatch = FakeDispatch(output="Workspace boundary changed.")
    router, ui, _, terminal, _, _, real_buf = make_router(
        dispatch=dispatch,
        workspace_fn=lambda: tmp_path,
    )

    router.route("/cwd set My\\ Project/")

    assert len(terminal.fns) == 1
    assert dispatch.calls[0][1] is router._real_console
    assert "Workspace boundary changed." in real_buf.getvalue()
    assert "Workspace boundary changed." not in ui._render_ansi()


def test_clear_action_clears_app_pane_after_terminal_confirmation() -> None:
    dispatch = FakeDispatch(action=SlashAction.CLEAR, output="Conversation cleared.")
    router, ui, _, terminal, _, _, _ = make_router(dispatch=dispatch)
    ui.show_user_message("old visible transcript")

    router.route("/clear")

    assert len(terminal.fns) == 1
    pane = ui._render_ansi()
    assert "old visible transcript" not in pane
    assert "Conversation cleared." in pane


def test_bang_command_runs_captured_in_pane() -> None:
    # `!<cmd>` runs captured on the worker and its output lands in the pane — NOT a
    # suspend-to-terminal flash (§31.17). The command is echoed first.
    shell = FakeShell(output="total 0\nfile.txt\n")
    router, ui, dispatch, terminal, manual_lines, _, _ = make_router(shell=shell)
    router.route("!ls -la")
    assert shell.commands == ["ls -la"]  # ran the stripped command, captured
    assert terminal.fns == []  # never suspended the app to the real terminal
    assert manual_lines == []  # the interactive loop is not entered
    assert dispatch.calls == []  # the dispatcher is never consulted for `!`
    pane = ui._render_ansi()
    assert "!ls -la" in pane  # echoed what ran
    assert "file.txt" in pane  # captured output rendered in the pane


def test_bang_command_strips_whitespace() -> None:
    shell = FakeShell()
    router, *_ = make_router(shell=shell)
    router.route("!   echo hi   ")
    assert shell.commands == ["echo hi"]


def test_bang_command_nonzero_exit_shows_code() -> None:
    shell = FakeShell(exit_code=2, output="boom\n")
    router, ui, _, _, _, _, _ = make_router(shell=shell)
    router.route("!false")
    pane = ui._render_ansi()
    assert "boom" in pane
    assert "exit code 2" in pane


def test_bare_bang_runs_manual_shell_via_terminal() -> None:
    # Bare `!` is the interactive shell loop, which still needs the real terminal.
    shell = FakeShell()
    router, _, _, terminal, manual_lines, _, _ = make_router(shell=shell)
    router.route("!")
    assert len(terminal.fns) == 1
    assert manual_lines == ["!"]
    assert shell.commands == []  # no captured one-shot for a bare `!`


def test_run_manual_command_captured_returns_output(tmp_path: Path) -> None:
    code, output = run_manual_command_captured("echo hello", tmp_path, None)
    assert code == 0
    assert output == "hello\n"


def test_run_manual_command_captured_merges_stderr_and_reports_exit(tmp_path: Path) -> None:
    # stderr is interleaved into the captured output; the real exit code is returned.
    code, output = run_manual_command_captured("echo oops 1>&2; exit 3", tmp_path, None)
    assert code == 3
    assert "oops" in output


def test_shell_command_drops_into_manual_shell() -> None:
    # /shell is needs_terminal; the dispatch returns MANUAL_SHELL, and the router
    # (already inside run_in_terminal) calls manual_shell("/shell").
    dispatch = FakeDispatch(action=SlashAction.MANUAL_SHELL, output="")
    router, _, _, terminal, manual_lines, _, _ = make_router(dispatch=dispatch)
    router.route("/shell")
    assert len(terminal.fns) == 1
    assert manual_lines == ["/shell"]


def test_busy_rejects_and_does_not_dispatch() -> None:
    router, ui, dispatch, terminal, manual_lines, exits, _ = make_router(is_busy=True)
    router.route("/help")
    assert dispatch.calls == []
    assert terminal.fns == []
    assert manual_lines == []
    assert exits == []
    assert "Busy" in ui._render_ansi()


def test_exit_action_calls_on_exit() -> None:
    dispatch = FakeDispatch(action=SlashAction.EXIT, output="")
    router, _, _, _, _, exits, _ = make_router(dispatch=dispatch)
    router.route("/help")  # fast path; dispatch returns EXIT
    assert exits == [1]


# ---------------------------------------------------------------------------
# needs_worker + the worker path (/plan revise runs a model turn)
# ---------------------------------------------------------------------------


def test_needs_worker_true_for_plan_revise_with_text() -> None:
    assert needs_worker("/plan revise make it shorter") is True
    assert needs_worker("/PLAN REVISE x") is True
    assert needs_worker("   /plan   revise   y ") is True


def test_needs_worker_false_otherwise() -> None:
    for line in ["/plan revise", "/plan", "/plan cancel", "/plan path", "/help", "", "hello"]:
        assert needs_worker(line) is False, line
    # /plan revise must NOT also be classified as a terminal command.
    assert needs_terminal("/plan revise make it shorter") is False


def test_plan_revise_routes_to_worker_not_loop_or_terminal() -> None:
    worker = FakeWorker(run=True)
    dispatch = FakeDispatch(output="")
    router, ui, _, terminal, manual_lines, _, _ = make_router(dispatch=dispatch, worker=worker)
    router.route("/plan revise make it shorter")
    # Routed to the worker — NOT loop-capture's run_in_terminal, NOT manual shell.
    assert len(worker.fns) == 1
    assert terminal.fns == []
    assert manual_lines == []
    # Echoed as a user message (mirrors a turn submission).
    assert "/plan revise make it shorter" in ui._render_ansi()
    # The worker fn dispatched against a CAPTURING console (not the real one).
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0][1] is not router._real_console


def test_plan_revise_without_text_stays_on_loop() -> None:
    worker = FakeWorker()
    router, _, dispatch, terminal, _, _, _ = make_router(worker=worker)
    router.route("/plan revise")  # no instruction → usage message, loop path
    assert worker.fns == []
    assert len(dispatch.calls) == 1  # loop-capture dispatch, not the worker
    assert terminal.fns == []


def test_busy_rejects_plan_revise() -> None:
    worker = FakeWorker()
    router, ui, _, _, _, _, _ = make_router(worker=worker, is_busy=True)
    router.route("/plan revise make it shorter")
    assert worker.fns == []
    assert "Busy" in ui._render_ansi()


# ---------------------------------------------------------------------------
# needs_background + the worker-display path (blocking network/IO, output→pane)
# ---------------------------------------------------------------------------


def test_needs_background_true_for_network_commands() -> None:
    assert needs_background("/model list") is True
    assert needs_background("/MODEL LIST") is True
    assert needs_background("/attach /tmp/cat.png") is True


def test_needs_background_false_otherwise() -> None:
    for line in ["/attach", "/model", "/model use gemma4:e4b", "/help", "/status", "", "hello"]:
        assert needs_background(line) is False, line
    # These run a blocking call but must NOT also be loop/terminal-classified.
    assert needs_terminal("/model list") is False
    assert needs_worker("/model list") is False


def test_background_command_runs_on_worker_and_captures_to_pane() -> None:
    worker = FakeWorker(run=True)
    dispatch = FakeDispatch(output="MODEL-TABLE")
    router, ui, _, terminal, manual_lines, _, real_buf = make_router(
        dispatch=dispatch, worker=worker
    )
    router.route("/model list")
    # Ran on the worker — NOT the loop-thread fast path, NOT run_in_terminal.
    assert len(worker.fns) == 1
    assert terminal.fns == []
    assert manual_lines == []
    # Captured against a capturing console (not the real terminal) and marshaled
    # into the pane; nothing went to the real terminal.
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0][1] is not router._real_console
    assert "MODEL-TABLE" in ui._render_ansi()
    assert "MODEL-TABLE" not in real_buf.getvalue()
    # A background DISPLAY command is NOT echoed as a user message (unlike a turn).
    assert "❯ /model list" not in ui._render_ansi()


def test_bare_attach_stays_on_loop() -> None:
    worker = FakeWorker()
    router, _, dispatch, terminal, _, _, _ = make_router(worker=worker)
    router.route("/attach")  # no path → in-memory list, loop path
    assert worker.fns == []
    assert len(dispatch.calls) == 1  # loop-capture dispatch
    assert terminal.fns == []


# ---------------------------------------------------------------------------
# AppUI.show_slash_output
# ---------------------------------------------------------------------------


def test_show_slash_output_appends_ansi_renderable() -> None:
    ui = AppUI(width_fn=lambda: 80)
    before = len(ui._renderables)
    ui.show_slash_output("\x1b[31mhello\x1b[0m\n")
    assert len(ui._renderables) == before + 1
    assert "hello" in ui._render_ansi()


def test_show_slash_output_closes_open_response() -> None:
    ui = AppUI(width_fn=lambda: 80)
    ui.stream_token("partial reply")
    ui.show_slash_output("DONE\n")
    # The open response was committed (closed) before the slash output appended.
    assert ui._open_response is None
    rendered = ui._render_ansi()
    assert "partial reply" in rendered
    assert "DONE" in rendered


def test_show_slash_output_ignores_blank() -> None:
    ui = AppUI(width_fn=lambda: 80)
    before = len(ui._renderables)
    ui.show_slash_output("\n")
    ui.show_slash_output("")
    assert len(ui._renderables) == before
