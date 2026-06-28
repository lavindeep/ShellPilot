"""Worker-thread turn execution and loop-thread UI marshaling (design section 31.13).

Branch 4 of the UI v2 rework. Today the runtime drives the UI synchronously on
the calling thread inside :meth:`ConversationRuntime.run_turn`; in the
full-screen app that thread is the prompt_toolkit event loop, so a long model
turn would freeze the whole UI (no repaint, no scroll, no Ctrl-C). This module
runs the one synchronous turn on a **worker thread** and marshals every UI
callback back onto the loop thread, so ``AppUI`` state and the pane repaint are
only ever touched on the loop thread.

Two pieces, both built to be driven synchronously in CI (the ``schedule``
callable is injected):

* :class:`ThreadedUI` — a ``RuntimeUI`` that wraps the real ``AppUI`` and
  enqueues every fire-and-forget content call onto the loop thread instead of
  running it inline. The blocking approval methods cannot be fire-and-forget
  (they return a value), so they delegate straight to the inner UI and run on
  the worker thread.
* :class:`TurnRunner` — owns the single worker thread for one turn and a
  ``busy`` flag that is only ever touched on the loop thread (no lock needed).
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from shellpilot.llm.client import GenerationCancelled

if TYPE_CHECKING:
    from prompt_toolkit.application import Application

    from shellpilot.cli.app_ui import AppUI
    from shellpilot.policy.approvals import ApprovalReply, ApprovalRequest
    from shellpilot.runtime.conversation import ConversationRuntime
    from shellpilot.runtime.events import RuntimeUI, TurnStats
    from shellpilot.runtime.planner import TaskPlan

# A zero-arg callback scheduled to run on the loop thread.
Scheduled = Callable[[], None]
# Marshals a loop-thread callback from a worker thread.
Schedule = Callable[[Scheduled], None]


class ThreadedUI:
    """A ``RuntimeUI`` that marshals every fire-and-forget call onto the loop thread.

    Wraps the real ``AppUI`` (``inner``) and a ``schedule`` callable that
    enqueues a zero-arg callback to run on the prompt_toolkit event-loop thread.
    The runtime calls these methods from the **worker** thread (see
    :class:`TurnRunner`); this wrapper guarantees ``AppUI`` state and the pane
    repaint are only ever touched on the loop thread.

    Args are captured with :func:`functools.partial` (never a closure over a
    loop variable) so a later call cannot rebind them before the queued call
    runs — ``stream_token("a")`` then ``stream_token("b")`` drain as ``"a"``,
    ``"b"``, not ``"b"``, ``"b"``.
    """

    def __init__(self, *, inner: RuntimeUI, schedule: Schedule) -> None:
        self._inner = inner
        self._schedule = schedule

    # ------------------------------------------------------------------
    # Fire-and-forget content methods — enqueued, never run inline.
    # ------------------------------------------------------------------

    def stream_token(self, token: str) -> None:
        self._schedule(functools.partial(self._inner.stream_token, token))

    # stream_thinking drives the live reasoning readout in AppUI (§31.14); it is
    # marshaled onto the loop thread like every other content call.
    def stream_thinking(self, text: str) -> None:
        self._schedule(functools.partial(self._inner.stream_thinking, text))

    # begin_response starts AppUI's turn-scoped thinking indicator (§31.14). No
    # args → the bound method is already a zero-arg Scheduled, so no partial is
    # needed.
    def begin_response(self) -> None:
        self._schedule(self._inner.begin_response)

    def end_response(self) -> None:
        self._schedule(self._inner.end_response)

    def turn_finished(self, stats: TurnStats) -> None:
        self._schedule(functools.partial(self._inner.turn_finished, stats))

    def show_status(self, text: str) -> None:
        self._schedule(functools.partial(self._inner.show_status, text))

    def show_error(self, text: str) -> None:
        self._schedule(functools.partial(self._inner.show_error, text))

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        self._schedule(functools.partial(self._inner.show_tool_call, name, arguments))

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._schedule(functools.partial(self._inner.show_tool_result, name, success, summary))

    def show_command_output(self, line: str) -> None:
        self._schedule(functools.partial(self._inner.show_command_output, line))

    def show_plan_progress(self, plan: TaskPlan) -> None:
        self._schedule(functools.partial(self._inner.show_plan_progress, plan))

    # ------------------------------------------------------------------
    # Blocking methods — return a value, so they CANNOT be fire-and-forget.
    # They delegate straight to the inner UI and run on the worker thread.
    # ------------------------------------------------------------------

    # NOTE: branch 7 replaces these with a thread-safe Future focus-swap
    # handshake (marshal the prompt to the loop thread, block the worker on a
    # Future for the reply). Until then the inner AppUI raises NotImplementedError
    # here — no approval may silently default — and the worker's try/except
    # surfaces that as a pane error (see TurnRunner._run).
    def ask_approval(self, request: ApprovalRequest) -> ApprovalReply:
        return self._inner.ask_approval(request)

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        return self._inner.ask_plan_approval(plan, path)


class TurnRunner:
    """Owns the single worker thread for one turn and the loop-thread busy flag.

    **Threading invariant (the core safety property of branch 4):** ``_busy`` is
    SET in :meth:`start` (called on the loop thread — it is the dock-submit
    handler) and CLEARED in :meth:`_mark_done` (scheduled back onto the loop
    thread from the worker's ``finally`` block). It is therefore only ever read
    or written on the loop thread, so no lock is needed. The worker thread
    (:meth:`_run`) never touches ``_busy`` or the inner UI directly — it only
    calls ``conversation.run_turn`` (whose UI is the marshaling
    :class:`ThreadedUI`, so every UI call it makes is already marshaled) and
    routes its own error/completion through ``schedule``.

    ``schedule`` is injected so CI can drive it synchronously; the real app uses
    :meth:`schedule`, which reads :attr:`app` lazily (set after construction) so
    the construction cycle — schedule needs app, app needs ``start``, the runner
    needs the conversation — is broken by deferred attribute assignment.
    """

    def __init__(self, *, inner_ui: AppUI, schedule: Schedule | None = None) -> None:
        # The raw content UI (NOT the marshaling ThreadedUI): the worker calls
        # show_error / abort_turn on it via schedule. Typed AppUI because the
        # cancel path needs abort_turn, which is an app-side AppUI method (like
        # show_user_message) and deliberately not on the RuntimeUI protocol.
        self._inner_ui = inner_ui
        # busy: only ever touched on the loop thread (see class docstring).
        self._busy = False
        # The worker handle, kept so a test (and branch-6 cancellation) can join
        # it. Spawned fresh per turn.
        self._thread: threading.Thread | None = None
        # Branch-6 per-turn cancel event (§31.15): created fresh in start(),
        # signaled by request_cancel() on the loop thread, observed by the
        # worker's model call. None when no turn is in flight.
        self._cancel: threading.Event | None = None
        # Set after construction, before app.run(), to break the build cycle;
        # read only during/after run().
        self.app: Application[None] | None = None
        self.conversation: ConversationRuntime | None = None
        # Default to the loop-marshaling schedule (reads self.app lazily); CI
        # injects a synchronous one.
        self._schedule: Schedule = schedule if schedule is not None else self.schedule

    def schedule(self, fn: Scheduled) -> None:
        """Marshal ``fn`` onto the loop thread, then request one repaint.

        Reads :attr:`app` lazily so the construction cycle is broken (``app`` is
        set after the app is built). prompt_toolkit coalesces the per-call
        ``invalidate()`` into one render tick, so per-token marshal+invalidate is
        correct (no throttling needed — measured at the live test, branch 4).
        Fails closed when the app is not running (``app``/``loop`` is None).
        """
        app = self.app
        if app is None:
            return
        loop = app.loop
        if loop is None:
            # NOTE: the app is shutting down — run_async clears app.loop on exit. A
            # turn still in flight at /exit drops its final _mark_done here, leaving
            # _busy True in an already-dead app (harmless for this opt-in dev entry).
            # On promotion to the shipping default, drain/join the worker on exit.
            return

        def _apply() -> None:
            fn()
            app.invalidate()

        loop.call_soon_threadsafe(_apply)

    def start(self, text: str) -> None:
        """Spawn the worker for one turn. Runs on the loop thread (dock submit).

        Ignores the call when a turn is already in flight — single model, single
        conversation, single worker; no parallel turns. NOTE: branch 9 adds the
        one-message queue + Up-arrow recall; until then a submit-while-busy is
        dropped.

        A fresh cancel ``Event`` is created here (before the worker spawns, so the
        worker sees it) and passed into ``run_turn``; ``request_cancel`` signals
        it from the loop thread to abort the turn (branch 6, §31.15).
        """
        if self._busy:
            return
        self._busy = True
        cancel = threading.Event()
        self._cancel = cancel
        # Pass the event as a thread ARG (a captured local), not via self._cancel,
        # so the worker never reads the mutable instance attribute. `request_cancel`
        # sets the SAME object from the loop thread, so the worker's local sees it —
        # but the threading invariant ("the worker only touches its args + schedule;
        # self._cancel/_busy are loop-thread-only") then holds literally, without
        # relying on the _busy guard to keep self._cancel from being reassigned.
        self._thread = threading.Thread(target=self._run, args=(text, cancel), daemon=True)
        self._thread.start()

    def request_cancel(self) -> bool:
        """Signal the in-flight turn to abort. Runs on the loop thread (Ctrl-C).

        Returns True when a turn was in flight (the cancel event is now set, and
        the worker's next model-call read boundary will raise
        ``GenerationCancelled``); False when idle (nothing to cancel — the caller
        then shows the idle hint). ``_busy``/``_cancel`` are read on the loop
        thread; ``Event.set()`` is the one thread-safe cross-thread signal to the
        worker (raw mode disables ISIG, so Ctrl-C is a key press, not a SIGINT).
        """
        if self._busy and self._cancel is not None:
            self._cancel.set()
            return True
        return False

    def _run(self, text: str, cancel: threading.Event) -> None:
        """Worker-thread body: run ONE synchronous turn off the loop thread.

        ``cancel`` is the per-turn event, received as an ARG (a captured local) so
        the worker never reads ``self._cancel`` — keeping the threading invariant
        literal. Touches nothing on the loop thread directly — only ``run_turn``
        (its UI is the :class:`ThreadedUI`, so every UI call is marshaled) and
        ``schedule``. A ``GenerationCancelled`` (the user hit Ctrl-C mid-stream)
        is a CLEAN abort, routed to ``abort_turn`` — never the error path. Any
        OTHER ``run_turn`` exception (an approval-needing turn raises
        ``NotImplementedError`` until branch 7, or an ``OllamaError`` surfaces) is
        rendered as a pane error instead of silently killing the daemon thread.
        The ``finally`` clears ``busy`` on the loop thread on EVERY path
        (success/cancel/error) so a turn never wedges the app.

        NOTE (branch 6b — subprocess killpg): a cancel requested while a tool
        COMMAND is running does NOT kill that subprocess here; the command
        finishes/times out, then the NEXT model call's stream-read sees the set
        event and raises GenerationCancelled, aborting the turn. Killing the
        running subprocess is branch 6b.
        """
        try:
            conversation = self.conversation
            if conversation is None:
                # Build-order violation (conversation must be set before start()).
                # Raise INSIDE the try so it surfaces as a pane error and the
                # finally still clears busy — never a silent daemon death + wedge.
                raise RuntimeError("TurnRunner.conversation not set before start()")
            conversation.run_turn(text, cancel=cancel)
        except GenerationCancelled:
            # Clean Ctrl-C abort, NOT a failure: clear the dangling indicator and
            # mark the partial aborted. The partial assistant reply was never
            # recorded in history (run_turn let the exception propagate before the
            # record site), so this is a display-only finalization.
            self._schedule(self._inner_ui.abort_turn)
        except Exception as exc:  # noqa: BLE001 - surface ANY worker failure to the pane
            self._schedule(functools.partial(self._inner_ui.show_error, f"Turn failed: {exc}"))
        finally:
            self._schedule(self._mark_done)

    def _mark_done(self) -> None:
        """Clear the busy flag. Scheduled onto the loop thread, so the flag is
        only ever mutated there (paired with the set in :meth:`start`)."""
        self._busy = False
