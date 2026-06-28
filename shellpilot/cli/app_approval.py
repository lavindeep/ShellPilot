"""Approval focus-swap rendezvous for the full-screen app (design section 31.16).

The full-screen app runs one conversation turn on a worker thread while the
prompt_toolkit event loop owns the main thread (see ``app_turn.py``). Every
fire-and-forget UI call is marshaled loop-ward, but ``ask_approval`` /
``ask_plan_approval`` RETURN a value, so they cannot be fire-and-forget. This is
the focus-swap handshake: the worker blocks on a ``concurrent.futures.Future``
while the loop thread renders the prompt into the pane, reads the user's
keystrokes in the dock, and resolves the future.

The pure parse helpers (:func:`parse_command_choice` / :func:`parse_plan_choice`)
carry the three-way y/e/n + HIGH typed-"run" contract and are unit-tested
directly, with no threading.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shellpilot.cli.render import _sanitize_line, approval_choices, plan_choices
from shellpilot.cli.theme import UNICODE_GLYPHS, Glyphs
from shellpilot.policy.approvals import APPROVE, DECLINE, ApprovalReply
from shellpilot.policy.risk import RiskLevel

if TYPE_CHECKING:
    from shellpilot.cli.app_turn import Schedule
    from shellpilot.cli.app_ui import AppUI
    from shellpilot.policy.approvals import ApprovalRequest
    from shellpilot.runtime.planner import TaskPlan


class _NeedSteer:
    """Sentinel: the user chose [e]dit at the choice phase; read steering next."""


NEED_STEER = _NeedSteer()


def parse_command_choice(request: ApprovalRequest, answer: str) -> ApprovalReply | _NeedSteer:
    """Map a command/tool approval keystroke to its three-way outcome.

    A HIGH-risk COMMAND requires the literal "run" to execute (the typed-"run"
    gate, design section 14.6); [e]dit steers without running and anything else
    declines. Every other request takes the y/e/n path.
    """
    low = answer.strip().lower()
    if request.risk is RiskLevel.HIGH and request.kind == "command":
        if low == "run":
            return APPROVE
        if low in ("e", "edit"):
            return NEED_STEER
        return DECLINE
    if low in ("y", "yes"):
        return APPROVE
    if low in ("e", "edit"):
        return NEED_STEER
    return DECLINE


def parse_plan_choice(answer: str) -> str | None:
    """Map a plan-approval keystroke to 'y' / 'e' / 'n', or None to re-prompt.

    Mirrors :meth:`TerminalUI.ask_plan_approval`: y/e/n plus empty→n; an
    unrecognized non-empty token re-prompts (its loop), modeled here as None.
    """
    low = answer.strip().lower()
    if low in ("y", "yes"):
        return "y"
    if low in ("e", "edit"):
        return "e"
    if low in ("n", "no", ""):
        return "n"
    return None


@dataclass
class _Pending:
    """The in-flight prompt. Loop-thread-only (see :class:`ApprovalGate`)."""

    future: Future[object]
    # feed(line) -> True when the future was resolved (prompt done), False when
    # another input line is wanted (steer/revision phase, or a re-prompt).
    feed: Callable[[str], bool]
    # Resolve as decline (Ctrl-C/EOF during approval cancels THIS action only).
    on_cancel: Callable[[], None]


class ApprovalGate:
    """Thread-safe approval rendezvous (design section 31.16).

    ``_pending`` is touched ONLY on the loop thread: :meth:`_enter_command` /
    :meth:`_enter_plan` set it (scheduled loop-ward by the worker), and
    :meth:`submit` / :meth:`cancel` read+clear it (called from keybindings, which
    run on the loop thread). The worker thread only ever touches the ``Future``
    (``result()`` blocks it; the loop thread's ``set_result`` unblocks it) — both
    are thread-safe stdlib primitives, so no lock is needed.

    NOTE: a pending approval at app exit leaves the daemon worker blocked on
    result(); harmless (process exit reaps it). On promotion to the shipping
    default, resolve pending approvals as DECLINE on exit.
    """

    def __init__(self, *, ui: AppUI, schedule: Schedule, glyphs: Glyphs = UNICODE_GLYPHS) -> None:
        self._ui = ui
        self._schedule = schedule
        self._glyphs = glyphs
        self._pending: _Pending | None = None

    # ------------------------------------------------------------------
    # Worker-thread entry points — block on the future.
    # ------------------------------------------------------------------

    def ask_command(self, request: ApprovalRequest) -> ApprovalReply:
        future: Future[object] = Future()
        self._schedule(functools.partial(self._enter_command, request, future))
        result = future.result()  # BLOCKS the worker thread
        assert isinstance(result, ApprovalReply)
        return result

    def ask_plan(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        future: Future[object] = Future()
        self._schedule(functools.partial(self._enter_plan, plan, path, future))
        result = future.result()  # BLOCKS the worker thread
        assert isinstance(result, tuple)
        return result

    # ------------------------------------------------------------------
    # Loop-thread prompt setup (scheduled by the worker entry points).
    # ------------------------------------------------------------------

    def _echo(self, line: str) -> None:
        # Keep the accepted input visible after the dock clears. show_status
        # re-sanitizes, so user-controlled text never reaches the pane raw; it
        # also (unlike show_user_message) does NOT reset the live turn indicator.
        self._ui.show_status(f"  {self._glyphs.chevron} {_sanitize_line(line)}")

    def _enter_command(self, request: ApprovalRequest, future: Future[object]) -> None:
        # Fail closed: a render sink raising on the loop thread must resolve the
        # worker's Future (via set_exception) instead of leaving it blocked on
        # result() forever — the turn then ends through TurnRunner._run's except
        # ("Turn failed") rather than wedging the app. Never approves by accident.
        try:
            self._ui.show_approval(request)
            self._ui.show_choices(approval_choices(request))
        except Exception as exc:  # noqa: BLE001 - never leave the worker hung
            self._pending = None
            if not future.done():
                future.set_exception(exc)
            return
        phase = {"steer": False}

        def feed(line: str) -> bool:
            if phase["steer"]:
                # Empty steer = plain decline (matches TerminalUI._read_steer).
                self._echo(line)
                future.set_result(ApprovalReply(approved=False, steer_text=line.strip() or None))
                return True
            decision = parse_command_choice(request, line)
            if isinstance(decision, _NeedSteer):
                phase["steer"] = True
                self._ui.show_status("  Tell the model what to do instead:")
                return False
            self._echo(line)
            future.set_result(decision)
            return True

        self._pending = _Pending(future, feed, lambda: future.set_result(DECLINE))

    def _enter_plan(self, plan: TaskPlan, path: str, future: Future[object]) -> None:
        # Fail closed (see _enter_command): a render sink raising here resolves
        # the worker's Future rather than wedging it on result().
        try:
            self._ui.show_plan_approval(plan, path)
            self._ui.show_choices(plan_choices())
        except Exception as exc:  # noqa: BLE001 - never leave the worker hung
            self._pending = None
            if not future.done():
                future.set_exception(exc)
            return
        phase = {"revision": False}

        def feed(line: str) -> bool:
            if phase["revision"]:
                self._echo(line)
                future.set_result(("e", line.strip()))
                return True
            choice = parse_plan_choice(line)
            if choice is None:
                # Re-prompt on an unparseable answer (matches the TerminalUI loop).
                self._ui.show_choices(plan_choices())
                return False
            if choice == "e":
                phase["revision"] = True
                self._ui.show_status("  Describe the changes you want:")
                return False
            self._echo(line)
            future.set_result((choice, ""))
            return True

        self._pending = _Pending(future, feed, lambda: future.set_result(("n", "")))

    # ------------------------------------------------------------------
    # Loop-thread public surface — driven by the dock keybindings.
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._pending is not None

    def submit(self, line: str) -> None:
        pending = self._pending
        if pending is None:
            return
        # Fail closed: feed() echoes via show_status BEFORE resolving the Future,
        # so a sink raising there must still resolve it — never leave the worker
        # blocked on result().
        try:
            done = pending.feed(line)
        except Exception as exc:  # noqa: BLE001 - never leave the worker hung
            self._pending = None
            if not pending.future.done():
                pending.future.set_exception(exc)
            return
        if done:
            self._pending = None

    def cancel(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        pending.on_cancel()
