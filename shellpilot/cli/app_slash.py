"""Slash / manual-shell routing for the full-screen app (design section 31.17).

The dock-submit keybinding sends every ``/`` and ``!`` line here on the LOOP
thread (the prompt_toolkit event loop). The event loop owns the terminal and
must never block, so this router splits the work two ways:

* **Fast, display-only** slash commands run on the loop thread against a fresh
  pane-capturing :class:`~rich.console.Console`; the captured ANSI is pushed
  into the pane (``AppUI.show_slash_output``).
* **Interactive / slow / own-stdout / manual-shell** commands run via
  ``run_in_terminal`` (the app suspends, the real terminal is restored, the
  handler runs synchronously, then the app redraws) so ``confirm()`` /
  cloud-consent ``input()`` and slow model preload never freeze the TUI.
  :func:`~shellpilot.cli.slash.needs_terminal`
  classifies which form needs the real terminal.

The router is built to be testable by INJECTING its effects (``dispatch``,
``run_terminal``, ``manual_shell``, ``on_exit``, ``is_busy``) — no running
prompt_toolkit app is needed in CI.
"""

from __future__ import annotations

import io
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import IO, cast

from rich.console import Console

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.slash import SlashAction, needs_background, needs_terminal, needs_worker
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, Glyphs


class _TeeWriter:
    """Write terminal command output to the real console and a capture buffer."""

    def __init__(self, primary: IO[str], capture: io.StringIO) -> None:
        self._primary = primary
        self._capture = capture
        self.encoding = str(getattr(primary, "encoding", "utf-8"))

    def write(self, text: str) -> int:
        written = self._primary.write(text)
        self._capture.write(text)
        return written

    def flush(self) -> None:
        self._primary.flush()

    def isatty(self) -> bool:
        return self._primary.isatty()

    def writable(self) -> bool:
        return True


def _split_model_use(line: str) -> list[str] | None:
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) < 2 or parts[0].lower() != "/model" or parts[1].lower() != "use":
        return None
    return parts


class SlashRouter:
    """Routes a dock-submitted ``/`` or ``!`` line to the right execution context.

    ``dispatch(line, console)`` runs the app's single ``SlashDispatcher`` against
    the GIVEN console (and the right confirm — see the wiring's loop-path
    ``_decline`` safety net): the loop path passes a capturing console, the
    terminal path passes ``real_console``. ``run_terminal`` schedules a zero-arg
    fn under ``run_in_terminal``; ``manual_shell`` runs a ``!``/``/shell`` line on
    the real terminal; ``on_exit`` exits the app; ``is_busy`` reports whether a
    turn is in flight (a slash is rejected while busy).
    """

    def __init__(
        self,
        *,
        ui: AppUI,
        dispatch: Callable[[str, Console], SlashAction],
        real_console: Console,
        width_fn: Callable[[], int],
        run_terminal: Callable[[Callable[[], None]], None],
        run_worker: Callable[[Callable[[], None]], bool],
        schedule: Callable[[Callable[[], None]], None],
        manual_shell: Callable[[str], None],
        run_shell: Callable[[str], tuple[int, str]],
        on_exit: Callable[[], None],
        is_busy: Callable[[], bool],
        workspace_fn: Callable[[], Path] | None = None,
        model_use_needs_terminal: Callable[[str], bool] | None = None,
        glyphs: Glyphs = UNICODE_GLYPHS,
    ) -> None:
        self._ui = ui
        self._dispatch = dispatch
        self._real_console = real_console
        self._width_fn = width_fn
        self._run_terminal = run_terminal
        self._run_worker = run_worker
        # Marshal a callback from the worker thread back onto the loop thread (for
        # the worker path's captured output → pane); = TurnRunner.schedule.
        self._schedule = schedule
        # Bare `!` / `/shell` → the interactive manual-shell loop (real terminal).
        self._manual_shell = manual_shell
        # One-shot `!<cmd>` → run captured, returns (exit_code, output) for the pane.
        self._run_shell = run_shell
        self._on_exit = on_exit
        self._is_busy = is_busy
        self._workspace_fn = workspace_fn or Path.cwd
        self._model_use_needs_terminal = model_use_needs_terminal or (lambda _line: True)
        self._glyphs = glyphs

    def route(self, line: str) -> None:
        # Called on the LOOP thread from the dock submit keybinding.
        stripped = line.strip()
        # NOTE: defense-in-depth. Since §31.18 the dock QUEUES a slash submitted
        # while busy and fires it (via _fire_pending) only after the turn ends, so
        # route() is reached from the dock only when idle. This guard stays as a
        # fail-safe for any non-dock caller.
        if self._is_busy():
            self._ui.show_status("Busy — finish or cancel the current turn first.")
            return
        if stripped.startswith("!"):
            command = stripped[1:].strip()
            if command:
                # One-shot `!<cmd>` → run captured on the worker and render its
                # output in the pane (no app suspend, no terminal flash, §31.17).
                # Echo the command first like a turn submission so the transcript
                # shows what ran. The loop thread must not block on the subprocess.
                self._ui.show_user_message(stripped)
                self._run_worker(lambda: self._dispatch_shell(command))
            else:
                # Bare `!` → the interactive manual shell, which needs the real
                # terminal (it reads live stdin), so it suspends the app.
                self._run_terminal(lambda: self._manual_shell(stripped))
            return
        if needs_worker(stripped) or needs_background(stripped):
            # Off the loop thread, both via TurnRunner.start_action:
            #   * needs_worker (/plan revise) — a model turn: its output reaches the
            #     pane via the runtime UI and approvals use the focus-swap gate, so
            #     echo the command first (mirrors a turn submission).
            #   * needs_background (/doctor, /model list, /attach <path>) — a
            #     non-interactive blocking network or filesystem I/O call: NO echo;
            #     its captured output IS the result and is marshaled to the pane.
            # The loop thread must never block on either. Read the width here (loop
            # thread) and pass it in — get_app() is unavailable on the worker.
            if needs_worker(stripped):
                self._ui.show_user_message(stripped)
            width = self._width_fn()
            self._run_worker(lambda: self._dispatch_worker(stripped, width))
            return
        if self._cwd_set_can_run_in_pane(stripped):
            self._dispatch_loop(stripped)
            return
        if self._model_use_can_run_in_pane(stripped):
            width = self._width_fn()
            self._run_worker(lambda: self._dispatch_worker(stripped, width))
            return
        if self._model_use_can_copy_terminal_output(stripped):
            width = self._width_fn()
            self._run_terminal(
                lambda: self._dispatch_terminal(stripped, copy_output=True, width=width)
            )
            return
        if needs_terminal(stripped):
            self._run_terminal(lambda: self._dispatch_terminal(stripped))
            return
        self._dispatch_loop(stripped)  # fast display command — capture into the pane

    def _cwd_set_can_run_in_pane(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError:
            return True
        if len(parts) < 2 or parts[0].lower() != "/cwd" or parts[1].lower() != "set":
            return False
        if len(parts) < 3:
            return True
        candidate = Path(parts[2]).expanduser()
        if not candidate.is_absolute():
            candidate = (self._workspace_fn() / parts[2]).resolve()
        return not candidate.is_dir()

    def _model_use_can_copy_terminal_output(self, line: str) -> bool:
        parts = _split_model_use(line)
        return parts is not None and self._model_use_needs_terminal(line)

    def _model_use_can_run_in_pane(self, line: str) -> bool:
        parts = _split_model_use(line)
        if parts is None:
            return False
        if len(parts) < 3:
            return True
        return not self._model_use_needs_terminal(line)

    def _capturing_console(self, width: int) -> tuple[Console, io.StringIO]:
        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            theme=SHELLPILOT_THEME,
            width=width,
        )
        return console, buf

    def _dispatch_loop(self, line: str) -> None:
        # Fast, non-interactive: run on the loop thread with a fresh capturing
        # console at the current pane width; push captured output to the pane.
        console, buf = self._capturing_console(self._width_fn())
        action = self._dispatch(line, console)
        self._ui.show_slash_output(buf.getvalue())
        self._after_action(action)

    def _dispatch_worker(self, line: str, width: int) -> None:
        # Runs on the WORKER thread (a /plan revise turn or a slow /model list //
        # /attach <path>). Capture the dispatcher's console and marshal any output
        # to the pane on the loop thread. For /plan revise the captured output is a
        # blank line (the turn's real output streams via the runtime's marshaling
        # UI during the call); for the background commands it IS the result.
        console, buf = self._capturing_console(width)
        action = self._dispatch(line, console)
        output = buf.getvalue()
        self._schedule(lambda: self._deliver_worker(output, action))

    def _deliver_worker(self, output: str, action: SlashAction) -> None:
        # Loop thread (marshaled from _dispatch_worker): push the captured output
        # (blank is ignored by show_slash_output), then handle the action.
        self._ui.show_slash_output(output)
        self._after_action(action)

    def _dispatch_shell(self, command: str) -> None:
        # WORKER thread: run one `!<cmd>` capturing its combined output, then
        # marshal the result to the pane on the loop thread (§31.17). Blocking the
        # worker (not the loop) keeps a slow command from freezing the UI.
        exit_code, output = self._run_shell(command)
        self._schedule(lambda: self._deliver_shell(exit_code, output))

    def _deliver_shell(self, exit_code: int, output: str) -> None:
        # Loop thread: render captured output through the sanitizing command-output
        # sink (per line, control chars stripped), then a dim exit-code note when
        # the command failed — mirroring the manual-shell loop's own feedback.
        for line in output.splitlines():
            self._ui.show_command_output(line)
        if exit_code != 0:
            self._ui.show_status(f"exit code {exit_code}")

    def _dispatch_terminal(
        self, line: str, *, copy_output: bool = False, width: int | None = None
    ) -> None:
        # Runs inside run_in_terminal (app suspended, real terminal available).
        # The dispatch is given the REAL console, so confirm()/consent input() work.
        if not copy_output:
            action = self._dispatch(line, self._real_console)
            self._after_action(action)
            return

        buf = io.StringIO()
        console = Console(
            file=cast(IO[str], _TeeWriter(self._real_console.file, buf)),
            force_terminal=True,
            color_system="truecolor",
            theme=SHELLPILOT_THEME,
            width=width or self._width_fn(),
        )
        action = self._dispatch(line, console)
        self._ui.show_slash_output(buf.getvalue())
        self._after_action(action)

    def _after_action(self, action: SlashAction) -> None:
        if action is SlashAction.EXIT:
            self._on_exit()
        elif action is SlashAction.CLEAR:
            self._ui.clear_conversation("Conversation cleared.")
        elif action is SlashAction.MANUAL_SHELL:
            # /shell → drop into the manual shell. Already inside run_in_terminal
            # (needs_terminal('/shell') is True), so call it directly.
            self._manual_shell("/shell")
