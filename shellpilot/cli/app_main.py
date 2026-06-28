"""Runnable entry for the full-screen app (design section 31.13).

This is the LIVE glue that constructs the full-screen ``Application``, drives a
:class:`~shellpilot.runtime.conversation.ConversationRuntime` through a
worker-thread :class:`~shellpilot.cli.app_turn.TurnRunner`, and ``app.run()``s
it. It is the DEFAULT path for an interactive TTY (``run_interactive`` selects it
unless ``--legacy-ui`` / ``SHELLPILOT_UI=legacy`` is set or the session is
non-TTY, in which case the legacy line-based REPL runs instead).

The construction cycle is broken by deferred attribute assignment — see the
ordering note in :func:`run_app`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from shellpilot.cli.app import StatusValues, build_app
from shellpilot.cli.app_turn import TurnRunner

# Active-turn indicator animation cadence (§31.14). Only fires while a turn is in
# flight, so the cost is bounded to when the app is genuinely working; idle ticks
# do no work (the loop just sleeps). ~0.15s matches the plane-glide FRAME_SECONDS.
_REFRESH_SECONDS = 0.15

if TYPE_CHECKING:
    from shellpilot.cli.app_approval import ApprovalGate
    from shellpilot.cli.app_ui import AppUI
    from shellpilot.cli.theme import Glyphs
    from shellpilot.runtime.conversation import ConversationRuntime


def run_app(
    runtime: ConversationRuntime,
    runner: TurnRunner,
    app_ui: AppUI,
    *,
    workspace: Path,
    model: str,
    profile: str,
    glyphs: Glyphs,
    commands: Sequence[str],
    is_cloud: bool = False,
    ctx_pct: int = 0,
    approval_gate: ApprovalGate | None = None,
    on_slash: Callable[[str], None] | None = None,
    is_busy: Callable[[], bool] | None = None,
    register_idle: Callable[[Callable[[], None]], None] | None = None,
    status_fn: Callable[[], StatusValues] | None = None,
) -> int:
    """Build the full-screen app around an already-wired conversation and run it.

    The caller has already chosen the conversation's UI as the marshaling
    :class:`ThreadedUI` whose inner is ``app_ui`` and whose schedule is
    ``runner.schedule`` (so the runtime's plan tools captured the marshaling
    bound methods at construction). This function only completes the wiring:

    1. ``app_ui`` was built first (its ``width_fn`` reads ``get_app()`` lazily,
       so it needs no running app yet); ``runner`` was built next (its
       ``schedule`` reads ``runner.app`` lazily); the conversation was built
       with the ``ThreadedUI`` over ``app_ui``.
    2. Build the ``Application`` from ``runner.start`` (the dock-submit handler)
       and ``app_ui`` (the pane source of truth).
    3. Set ``runner.app`` and ``runner.conversation`` AFTER the app exists and
       BEFORE ``app.run()`` — both are read only once a turn runs, by which time
       ``app.run()`` has set ``app.loop``.

    NOTE: ``run_app`` stays UI-only and does not itself write the ``session_end``
    audit event — the caller (``run_interactive``) writes it right after this
    returns, mirroring the legacy REPL so both UIs audit the session identically.
    Returns the app's exit code.
    """
    app = build_app(
        workspace=workspace,
        model=model,
        profile=profile,
        glyphs=glyphs,
        commands=commands,
        is_cloud=is_cloud,
        ctx_pct=ctx_pct,
        ui=app_ui,
        on_submit=runner.start,
        on_interrupt=runner.request_cancel,
        on_slash=on_slash,
        approval_gate=approval_gate,
        is_busy=is_busy,
        register_idle=register_idle,
        status_fn=status_fn,
    )
    runner.app = app
    runner.conversation = runtime

    async def _refresh_loop() -> None:
        # Animate the live thinking indicator while a turn runs; when idle, do
        # nothing (redraws stay event-driven) so the app spends no CPU sitting at
        # the prompt — replacing prompt_toolkit's unconditional refresh_interval.
        while True:
            await asyncio.sleep(_REFRESH_SECONDS)
            if app_ui.is_animating:
                app.invalidate()

    def _start_refresh() -> None:
        # pre_run fires once the event loop is up, so create_background_task is safe;
        # prompt_toolkit cancels the task when the app exits.
        app.create_background_task(_refresh_loop())

    app.run(pre_run=_start_refresh)
    return 0
