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
  cloud-consent ``input()`` and ``run_doctor``'s own stdout work, and a slow
  model preload never freezes the TUI. :func:`~shellpilot.cli.slash.needs_terminal`
  classifies which form needs the real terminal.

The router is built to be testable by INJECTING its effects (``dispatch``,
``run_terminal``, ``manual_shell``, ``on_exit``, ``is_busy``) — no running
prompt_toolkit app is needed in CI.
"""

from __future__ import annotations

import io
from collections.abc import Callable

from rich.console import Console

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.slash import SlashAction, needs_background, needs_terminal, needs_worker
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, Glyphs


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
        on_exit: Callable[[], None],
        is_busy: Callable[[], bool],
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
        self._manual_shell = manual_shell
        self._on_exit = on_exit
        self._is_busy = is_busy
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
            # `!<cmd>` / bare `!` → manual shell, always via the real terminal.
            self._run_terminal(lambda: self._manual_shell(stripped))
            return
        if needs_worker(stripped) or needs_background(stripped):
            # Off the loop thread, both via TurnRunner.start_action:
            #   * needs_worker (/plan revise) — a model turn: its output reaches the
            #     pane via the runtime UI and approvals use the focus-swap gate, so
            #     echo the command first (mirrors a turn submission).
            #   * needs_background (/model list, /attach <path>) — a non-interactive
            #     blocking network/IO call: NO echo; its captured output IS the
            #     result and is marshaled to the pane.
            # The loop thread must never block on either. Read the width here (loop
            # thread) and pass it in — get_app() is unavailable on the worker.
            if needs_worker(stripped):
                self._ui.show_user_message(stripped)
            width = self._width_fn()
            self._run_worker(lambda: self._dispatch_worker(stripped, width))
            return
        if needs_terminal(stripped):
            self._run_terminal(lambda: self._dispatch_terminal(stripped))
            return
        self._dispatch_loop(stripped)  # fast display command — capture into the pane

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

    def _dispatch_terminal(self, line: str) -> None:
        # Runs inside run_in_terminal (app suspended, real terminal available).
        # The dispatch is given the REAL console, so confirm()/consent input() and
        # run_doctor's own stdout work.
        action = self._dispatch(line, self._real_console)
        self._after_action(action)

    def _after_action(self, action: SlashAction) -> None:
        if action is SlashAction.EXIT:
            self._on_exit()
        elif action is SlashAction.MANUAL_SHELL:
            # /shell → drop into the manual shell. Already inside run_in_terminal
            # (needs_terminal('/shell') is True), so call it directly.
            self._manual_shell("/shell")
