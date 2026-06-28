"""AppUI: RuntimeUI implementation that routes content into the full-screen app pane.

This is the content seam for UI v2 (design section 31.12). ``AppUI`` holds a list
of Rich renderables as the transcript source of truth, renders them to a single
ANSI string at the shared terminal width, and caches the result until the content
or the width changes. The pane control in ``app.py`` is a ``FormattedTextControl``
over ``ANSI(ui._render_ansi())``, so Rich handles all wrapping and theming at width
W while prompt_toolkit handles layout and scrolling.

``AppUI`` is a pure state-and-render object: it never calls ``get_app()`` or
``invalidate()`` — periodic repaint is the app's job (``refresh_interval``) and the
clock is injected (``time_fn``) — so it is fully testable without a running app.
"""

from __future__ import annotations

import io
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from shellpilot.cli.render import _sanitize_line, plan_step_line
from shellpilot.cli.render import tool_call as render_tool_call
from shellpilot.cli.render import tool_result as render_tool_result
from shellpilot.cli.streaming import phase_for_elapsed
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, Glyphs
from shellpilot.memory.redaction import redact_structure
from shellpilot.runtime.budget import CHARS_PER_TOKEN
from shellpilot.tools.base import workspace_display

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalReply, ApprovalRequest
    from shellpilot.runtime.events import TurnStats
    from shellpilot.runtime.planner import TaskPlan

# The plane glyph advances one track cell every FRAME_SECONDS of elapsed time, so
# the glide is a pure function of the (injected) clock — no animation thread.
FRAME_SECONDS = 0.15


def _fmt_count(n: int) -> str:
    """k/m abbreviation for token counts (design section 31.14).

    ``999`` → ``"999"``; ``1800`` → ``"1.8k"``; ``2_400_000`` → ``"2.4m"``. The
    1m check precedes the 1k check so a seven-figure count never renders as ``k``.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


@dataclass
class _TurnIndicator:
    """Turn-scoped live-frontier state (design section 31.14).

    ``start`` is the injected-clock timestamp of the turn's first model call;
    ``reasoning_chars`` accumulates the length of every thinking chunk. The token
    estimate is derived at render time (``ceil(chars / CHARS_PER_TOKEN)``,
    consistent with ``budget.estimate_tokens``) — chars are stored, not tokens.
    """

    start: float
    reasoning_chars: int = 0


class AppUI:
    """RuntimeUI implementation that routes content into the full-screen app pane.

    All methods must be called from the UI thread. Cross-thread marshaling and
    ``app.invalidate()`` are branch-4 concerns; this class stays pure state+render.
    """

    def __init__(
        self,
        *,
        glyphs: Glyphs = UNICODE_GLYPHS,
        workspace: Path | None = None,
        width_fn: Callable[[], int],
        show_reasoning: bool = True,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._glyphs = glyphs
        # Workspace for display-integrity (design section 14.5): when set, a
        # `path` argument in the tool-call line is shown as its resolved,
        # workspace-relative target — the SAME resolution the tool acts on.
        self._workspace = workspace
        self._width_fn = width_fn
        # Gate for the reasoning-token readout (settings.ui.show_reasoning_summary,
        # design section 31.14): when False, the live/done lines show plane+phrase+
        # timer only — no reasoning estimate, no total. Display-only; audit capture
        # of thinking is unaffected.
        self._show_reasoning = show_reasoning
        # Injected clock so the indicator's elapsed/animation is testable with a
        # fixed time_fn rather than the wall clock.
        self._time_fn = time_fn
        # Source of truth: every committed renderable in the transcript.
        self._renderables: list[RenderableType] = []
        # Accumulated token text for the current open response.
        # None means no response is open; an open response is the last renderable
        # in spirit but lives here until end_response() finalizes it.
        self._open_response: str | None = None
        # Turn-scoped live indicator (design section 31.14): None when idle, a
        # _TurnIndicator while a turn runs. begin_response starts it; turn_finished
        # freezes it to a permanent done line and clears it.
        self._indicator: _TurnIndicator | None = None
        # Width-keyed ANSI cache: (width, ansi_string), or None when stale.
        self._cache: tuple[int, str] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close_open_response(self) -> None:
        """Finalize the open response as a Markdown renderable, if one is open."""
        if self._open_response is not None:
            self._renderables.append(Markdown(_sanitize_line(self._open_response)))
            self._open_response = None
            self._cache = None

    def _add_renderable(self, renderable: RenderableType) -> None:
        """Close any open response first, then append a renderable to the transcript.

        Preserves ordering: response → tool call → response produces three distinct
        transcript entries (the open response closes around the tool call).
        """
        self._close_open_response()
        self._renderables.append(renderable)
        self._cache = None

    def _render_ansi(self) -> str:
        """Render the full transcript to an ANSI string at the current terminal width.

        The width is read from ``width_fn`` on every call and compared against the
        cache key; a resize automatically re-derives the ANSI (Rich re-wraps at the
        new width) and updates the cache. ``AppUI`` never calls ``get_app()``; the
        caller supplies the width source via ``width_fn`` at construction time.
        """
        width = self._width_fn()
        active = self._indicator is not None
        # An active indicator's elapsed/animation changes every render, so it
        # bypasses the width cache entirely (never read, never write). Idle
        # renders stay cached by width — a refresh tick with no active turn is a
        # cheap cache hit. NOTE: gating refresh on an active turn is the upgrade
        # path (branch 5) if profiling ever shows the idle tick costs anything.
        if not active and self._cache is not None and self._cache[0] == width:
            return self._cache[1]
        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            theme=SHELLPILOT_THEME,
            width=width,
        )
        renderables: list[RenderableType] = list(self._renderables)
        if self._open_response is not None:
            # Include in-progress response without committing it yet. Sanitize the
            # model text at the sink (mirrors ResponseStream, streaming.py:171) —
            # _sanitize_line keeps LF, so markdown structure survives.
            renderables.append(Markdown(_sanitize_line(self._open_response)))
        if active:
            # The live frontier line always renders LAST, below all completed
            # content, so it "moves down" as tool calls/responses append above it.
            renderables.append(self._indicator_line())
        for renderable in renderables:
            console.print(renderable)
        ansi = buf.getvalue()
        if not active:
            self._cache = (width, ansi)
        return ansi

    def _indicator_line(self) -> Text:
        """The live frontier line for the active turn (design section 31.14).

        ``{plane}  {phrase}… {N}s · {fmt(reasoning)} reasoning`` — the plane glides
        a track cell every ``FRAME_SECONDS`` and the phrase is a DETERMINISTIC pick
        from the current flight phase's pool (10-second buckets, not random), so
        the whole line is reproducible under a fixed ``time_fn``.
        """
        indicator = self._indicator
        assert indicator is not None  # only called from _render_ansi when active
        elapsed = self._time_fn() - indicator.start
        frames = self._glyphs.spinner_frames
        plane = frames[int(elapsed / FRAME_SECONDS) % len(frames)]
        pool = phase_for_elapsed(elapsed).pool
        phrase = pool[int(elapsed / 10) % len(pool)]
        line = Text()
        line.append(plane, style="sp.accent")
        line.append(f"  {phrase}{self._glyphs.ellipsis} {int(elapsed)}s", style="sp.dim")
        if self._show_reasoning:
            reasoning = math.ceil(indicator.reasoning_chars / CHARS_PER_TOKEN)
            line.append(f" · {_fmt_count(reasoning)} reasoning", style="sp.dim")
        return line

    # ------------------------------------------------------------------
    # RuntimeUI content methods — mirroring TerminalUI exactly
    # ------------------------------------------------------------------

    def stream_token(self, token: str) -> None:
        """Accumulate a streaming token into the open response."""
        if self._open_response is None:
            self._open_response = token
        else:
            self._open_response += token
        self._cache = None

    def end_response(self) -> None:
        """Close the open response so the next stream_token starts a fresh one."""
        self._close_open_response()

    def begin_response(self) -> None:
        # Turn-scoped: the FIRST model call of a turn starts the live indicator;
        # later calls within the same tool loop do NOT restart it (the elapsed
        # timer and reasoning count span the whole turn, not each model call).
        if self._indicator is None:
            self._indicator = _TurnIndicator(start=self._time_fn())
        self._cache = None

    def turn_finished(self, stats: TurnStats) -> None:
        # Freeze the live frontier into a permanent done line and clear the active
        # indicator. Elapsed comes from the runtime's authoritative stats.elapsed_s
        # (not the UI clock); reasoning is frozen from the accumulated chars; total
        # is the exact summed output-token count.
        # NOTE: only the success path freezes — a turn that errors before
        # turn_finished (or a Ctrl-C cancel) leaves the indicator active; that
        # turn-failure/cancel robustness is branch 6's.
        reasoning_chars = self._indicator.reasoning_chars if self._indicator is not None else 0
        self._indicator = None
        line = Text()
        line.append(self._glyphs.check, style="sp.accent")
        line.append(" done", style="sp.dim")
        line.append(f" · {int(stats.elapsed_s)}s", style="sp.dim")
        if self._show_reasoning:
            reasoning = math.ceil(reasoning_chars / CHARS_PER_TOKEN)
            line.append(f" · {_fmt_count(reasoning)} reasoning", style="sp.dim")
            line.append(f" · {_fmt_count(stats.output_tokens)} total", style="sp.dim")
        self._add_renderable(line)

    def stream_thinking(self, text: str) -> None:
        # The reasoning count climbs ONLY while the model is thinking, so it
        # freezes naturally when thinking stops and resumes if it thinks again. No
        # reasoning TEXT is ever rendered — only its char count, as a token
        # estimate. A no-op when no turn is active (defensive; begin_response runs
        # first in the runtime's tool loop).
        if self._indicator is not None:
            self._indicator.reasoning_chars += len(text)
            self._cache = None

    def show_user_message(self, text: str) -> None:
        # Echo the submitted user message into the transcript. App-side (NOT a
        # RuntimeUI protocol method) — the full-screen analogue of the REPL
        # leaving the typed line in scrollback. The user-controlled text is
        # control-char sanitized (display-integrity) before it reaches the pane.
        #
        # A new turn begins here, so discard any indicator left dangling by a
        # PRIOR turn that errored before turn_finished (the error was already
        # surfaced via show_error). Without this, the next begin_response would be
        # a no-op against the stale indicator and the new turn's timer would count
        # from the old turn's start. Full turn-failure/cancel UX is branch 6's; this
        # is the minimal guard so an error never poisons the following turn.
        self._indicator = None
        echo = f"{self._glyphs.chevron} {_sanitize_line(text)}"
        self._add_renderable(Text(echo, style="sp.accent"))

    def show_status(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.dim"))

    def show_error(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.error"))

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        # Redact secrets in the summary line so auto-approved tool calls never
        # expose credentials in the visible pane channel. A `path` argument is
        # shown as its resolved, workspace-relative target (the SAME resolution
        # the tool acts on) so the displayed path cannot be spoofed and matches
        # the file actually touched (design section 14.5).
        redacted = redact_structure(arguments)
        assert isinstance(redacted, dict)
        summary = ", ".join(
            f"{key}={self._tool_call_value(key, value)}" for key, value in redacted.items()
        )
        if len(summary) > 80:
            summary = summary[:79] + self._glyphs.ellipsis
        self._add_renderable(render_tool_call(name, summary, self._glyphs))

    def _tool_call_value(self, key: str, value: object) -> str:
        # A `path` argument is shown as its resolved, workspace-relative target
        # (display-integrity, design section 14.5). Without a workspace the value
        # renders verbatim — matching TerminalUI._tool_call_value exactly.
        if key == "path" and isinstance(value, str) and self._workspace is not None:
            return repr(workspace_display(self._workspace, value))
        return repr(value)

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._add_renderable(render_tool_result(success, summary, self._glyphs))

    def show_command_output(self, line: str) -> None:
        # "    " prefix + control-char sanitization + dim, no markup, no highlight —
        # mirroring TerminalUI (markup=False/highlight=False are implied by Text).
        self._add_renderable(Text("    " + _sanitize_line(line), style="sp.dim"))

    def show_plan_progress(self, plan: TaskPlan) -> None:
        # Uses plan_step_line (not plan_panel) with 2-cell left indent, then a blank
        # line — mirroring TerminalUI.show_plan_progress exactly.
        self._close_open_response()
        for index, step in enumerate(plan.steps, 1):
            self._renderables.append(
                Padding(plan_step_line(index, step, self._glyphs), (0, 0, 0, 2))
            )
        self._renderables.append(Text(""))  # blank line separator
        self._cache = None

    # ------------------------------------------------------------------
    # Approval stubs — raise until branch 7 wires the focus-swap
    # ------------------------------------------------------------------

    def ask_approval(self, request: ApprovalRequest) -> ApprovalReply:
        raise NotImplementedError("approval focus-swap is wired in branch 7")

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        raise NotImplementedError("approval focus-swap is wired in branch 7")
