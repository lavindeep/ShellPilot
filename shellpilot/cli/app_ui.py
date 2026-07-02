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
from typing import IO, TYPE_CHECKING, cast

from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.text import Text

from shellpilot.cli.render import (
    _sanitize_line,
    approval_cwd,
    approval_info,
    diff_row_count,
    plan_panel,
    plan_step_line,
    render_diff,
    response_markdown,
    tool_call_block,
)
from shellpilot.cli.render import tool_result as render_tool_result
from shellpilot.cli.streaming import DiffReveal, phase_for_elapsed
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, Glyphs
from shellpilot.memory.redaction import redact_structure
from shellpilot.runtime.budget import CHARS_PER_TOKEN
from shellpilot.tools.base import workspace_display

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalRequest
    from shellpilot.runtime.events import TurnStats
    from shellpilot.runtime.planner import TaskPlan

# The plane glyph advances one track cell every FRAME_SECONDS of elapsed time, so
# the glide is a pure function of the (injected) clock — no animation thread.
FRAME_SECONDS = 0.15

# A collapsed thinking trail shows this many non-blank reasoning lines; the rest
# fold behind a "+N hidden lines" footer until the trail is clicked (design §31.19).
TRAIL_COLLAPSED_LINES = 6


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


@dataclass
class _Trail:
    """An inline, display-only thinking trail for one reasoning phase (§31.19).

    ``text`` accumulates raw model thinking (display only — never fed back to the
    model or recorded in history). ``expanded`` is the per-trail collapse state
    that a CLICK on the trail toggles (toggle_at, §31.16). The collapsed view shows
    the first ``TRAIL_COLLAPSED_LINES`` non-blank lines; a footer reports the hidden
    remainder. No ``finished`` flag is needed — a trail stops accumulating the
    moment it is no longer ``AppUI._active_trail``.
    """

    text: str = ""
    expanded: bool = False


@dataclass
class _Diff:
    """A collapsible diff panel in the transcript (§31.16).

    ``diff_text`` is the raw unified diff; ``expanded`` is the per-diff collapse
    state. Collapsed shows the first ``DiffReveal.WINDOW_ROWS`` rows; expanded
    shows all. The toggle target is found by CLICK (the panel's transcript line
    range) or the Ctrl-O keyboard fallback (the latest diff). ``total_rows`` is
    the rendered row count, captured once at construction so the click/collapse
    hint is suppressed for a diff that already fits — no re-parse per frame.
    """

    diff_text: str
    total_rows: int
    expanded: bool = False


class _LineCountingWriter:
    """A text sink that tallies newlines as Rich writes through it.

    Lets ``_render_ansi`` record each element's transcript line range in the SAME
    single render pass (one Console, not one per element) — the render runs every
    refresh tick during an active turn, so the per-element-Console alternative
    would multiply Console construction by the transcript length each frame.
    """

    def __init__(self) -> None:
        self._buf = io.StringIO()
        self.lines = 0

    def write(self, text: str) -> int:
        self.lines += text.count("\n")
        return self._buf.write(text)

    def flush(self) -> None:
        self._buf.flush()

    def getvalue(self) -> str:
        return self._buf.getvalue()


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
        workspace_fn: Callable[[], Path] | None = None,
        width_fn: Callable[[], int],
        show_reasoning: bool = True,
        time_fn: Callable[[], float] = time.monotonic,
        intro: RenderableType | None = None,
    ) -> None:
        self._glyphs = glyphs
        # Workspace for display-integrity (design section 14.5): when set, a
        # `path` argument in the tool-call line is shown as its resolved,
        # workspace-relative target — the SAME resolution the tool acts on.
        # workspace_fn (preferred in production) is called at render time so a
        # mid-session /cwd change is immediately reflected; workspace is the
        # static fallback for test doubles that construct without a live runtime.
        self._workspace = workspace
        self._workspace_fn = workspace_fn
        self._width_fn = width_fn
        # Gate for the reasoning-token readout (settings.ui.show_reasoning_summary,
        # design section 31.14): when False, the live/done lines show plane+phrase+
        # timer only — no reasoning estimate, no total. Display-only; audit capture
        # of thinking is unaffected.
        self._show_reasoning = show_reasoning
        # Injected clock so the indicator's elapsed/animation is testable with a
        # fixed time_fn rather than the wall clock.
        self._time_fn = time_fn
        # Source of truth: every committed renderable in the transcript. A _Trail
        # entry is a live thinking block rendered via _render_trail (§31.19). The
        # boot banner (when provided) is seeded as the first transcript entry so it
        # renders inside the alt-screen pane — a console.print would be lost behind
        # the full-screen app (§31.13).
        self._renderables: list[RenderableType | _Trail | _Diff] = (
            [intro] if intro is not None else []
        )
        # Accumulated token text for the current open response.
        # None means no response is open; an open response is the last renderable
        # in spirit but lives here until end_response() finalizes it.
        self._open_response: str | None = None
        # Turn-scoped live indicator (design section 31.14): None when idle, a
        # _TurnIndicator while a turn runs. begin_response starts it; turn_finished
        # freezes it to a permanent done line and clears it.
        self._indicator: _TurnIndicator | None = None
        # Inline thinking trails (§31.19): _active_trail is the one currently
        # accumulating stream_thinking text (None between reasoning phases). A
        # trail is toggled by CLICKING it (toggle_at), so there is no latest-trail
        # pointer — older trails stay individually reachable. _active_trail is part
        # of _renderables; this is just a pointer into it.
        self._active_trail: _Trail | None = None
        # Latest diff in the transcript — the Ctrl-O keyboard fallback's target
        # (§31.16). Clicking reaches any diff; Ctrl-O only the most recent one.
        self._latest_diff: _Diff | None = None
        # Transcript line ranges of the click-toggleable elements (trails + diffs),
        # rebuilt whenever the ANSI is rebuilt (see _render_ansi). A pane click maps
        # its row → the element whose [start, end) range contains it (toggle_at).
        self._toggle_ranges: list[tuple[int, int, _Trail | _Diff]] = []
        # Width-keyed ANSI cache: (width, ansi_string), or None when stale.
        self._cache: tuple[int, str] | None = None
        # Whether the idle hint is already the last thing shown — so repeated
        # Ctrl-C while idle doesn't stack it (§31.17). Re-armed at each new turn.
        self._idle_hint_shown = False

    @property
    def is_animating(self) -> bool:
        """True while a turn's live indicator should keep animating (a turn is in
        flight). ``run_app``'s gated refresh loop polls this to invalidate the app
        ONLY during a turn, so an idle app schedules no timer redraws (§31.14)."""
        return self._indicator is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close_open_response(self) -> None:
        """Finalize the open response as a Markdown renderable, if one is open."""
        if self._open_response is not None:
            self._renderables.append(response_markdown(self._open_response))
            self._open_response = None
            self._cache = None

    def _finalize_active_trail(self) -> None:
        # Any non-thinking transcript content ends the active reasoning phase. The
        # trail STAYS in _renderables (finished trails remain visible and stay
        # click-toggleable individually, regardless of age); it just stops
        # accumulating. This is the ONLY cleanup the spec's "clear active unfinished
        # trail state, never reset prior finished trails" needs — we touch a single
        # pointer, never another trail's expanded state.
        self._active_trail = None

    def _add_renderable(self, renderable: RenderableType) -> None:
        """Close any open response first, then append a renderable to the transcript.

        Preserves ordering: response → tool call → response produces three distinct
        transcript entries (the open response closes around the tool call).
        """
        self._finalize_active_trail()
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
        writer = _LineCountingWriter()
        console = Console(
            # Rich only needs `.write`/`.flush`; the writer tallies lines as it goes.
            file=cast("IO[str]", writer),
            force_terminal=True,
            color_system="truecolor",
            theme=SHELLPILOT_THEME,
            width=width,
        )
        renderables: list[RenderableType | _Trail | _Diff] = list(self._renderables)
        if self._open_response is not None:
            # Include in-progress response without committing it yet. Sanitize the
            # model text at the sink (mirrors ResponseStream, streaming.py:171) —
            # _sanitize_line keeps LF, so markdown structure survives.
            renderables.append(response_markdown(self._open_response))
        if active:
            # The live frontier line always renders LAST, below all completed
            # content, so it "moves down" as tool calls/responses append above it.
            renderables.append(self._indicator_line())
        # Rebuild the click-toggle line index alongside the ANSI: a trail/diff
        # occupies document lines [start, end) and toggle_at(y) maps a pane click
        # back to it. Kept in lockstep with the ANSI we are about to return.
        ranges: list[tuple[int, int, _Trail | _Diff]] = []
        for renderable in renderables:
            start = writer.lines
            if isinstance(renderable, _Trail):
                console.print(self._render_trail(renderable))
                ranges.append((start, writer.lines, renderable))
            elif isinstance(renderable, _Diff):
                console.print(self._render_diff(renderable))
                ranges.append((start, writer.lines, renderable))
            else:
                console.print(renderable)
        self._toggle_ranges = ranges
        ansi = writer.getvalue()
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

    def _render_trail(self, trail: _Trail) -> Text:
        # Display-only thinking (§31.19), faint + indented, header carries the
        # reasoning-token estimate. Model-controlled text → EVERY line sanitized.
        # Blank lines are dropped so the 10-line cap counts real reasoning lines.
        lines = [ln for ln in trail.text.splitlines() if ln.strip()]
        reasoning = math.ceil(len(trail.text) / CHARS_PER_TOKEN)
        caret = (
            ("▾" if trail.expanded else "▸")
            if self._glyphs == UNICODE_GLYPHS
            else ("v" if trail.expanded else ">")
        )
        parts: list[Text] = [
            Text(f"{caret} thinking · {_fmt_count(reasoning)} reasoning", style="sp.dim")
        ]
        shown = lines if trail.expanded else lines[:TRAIL_COLLAPSED_LINES]
        parts.extend(Text("  " + _sanitize_line(ln), style="sp.faint") for ln in shown)
        if trail.expanded:
            parts.append(Text("  click to collapse", style="sp.faint"))
        else:
            hidden = len(lines) - len(shown)
            if hidden > 0:
                parts.append(
                    Text(
                        f"  {self._glyphs.ellipsis} +{hidden} hidden lines · click to expand",
                        style="sp.faint",
                    )
                )
        return Text("\n").join(parts)

    def _render_diff(self, d: _Diff) -> RenderableType:
        # Collapsible diff panel (§31.16). Standardized to the pane width (minus the
        # 2-col indent) with long lines folded; diff text is sanitized inside
        # render_diff/_diff_rows, so this is a safe sink. Collapsed caps at
        # DiffReveal.WINDOW_ROWS; expanded shows all rows. The click/Ctrl-O hint is
        # added only when the diff overflows the cap (nothing to toggle otherwise).
        panel = render_diff(
            d.diff_text,
            self._glyphs,
            width=max(1, self._width_fn() - 2),
            max_rows=None if d.expanded else DiffReveal.WINDOW_ROWS,
        )
        body: RenderableType = panel
        if d.total_rows > DiffReveal.WINDOW_ROWS:
            hint = "click or ctrl-o to collapse" if d.expanded else "click or ctrl-o to expand"
            body = Group(panel, Text(hint, style="sp.faint"))
        return Padding(body, (0, 0, 0, 2))

    # ------------------------------------------------------------------
    # RuntimeUI content methods — mirroring TerminalUI exactly
    # ------------------------------------------------------------------

    def stream_token(self, token: str) -> None:
        """Accumulate a streaming token into the open response."""
        # A response token does NOT end the reasoning phase (§31.19). A model may
        # emit a trailing thought AFTER the answer starts; that fragment must join
        # the SAME trail, not cut the answer into two blocks. The trail is finalized
        # at the model-call boundary (end_response) instead of on the first token.
        if self._open_response is None:
            self._open_response = token
        else:
            self._open_response += token
        self._cache = None

    def end_response(self) -> None:
        """Close the open response and finalize the call's reasoning trail (§31.19).

        end_response bounds one model call (conversation.py wraps each chat() in
        begin_response/end_response). Finalizing the active trail HERE — not on the
        first response token — is what keeps interleaved thinking/answer within a
        call as one trail + one response, while a genuine second reasoning phase in
        the NEXT call still opens its own fresh trail.
        """
        self._close_open_response()
        self._finalize_active_trail()

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

    def abort_turn(self) -> None:
        # Branch 6 (§31.15): a turn was cancelled mid-stream (Ctrl-C). App-side
        # (NOT a RuntimeUI protocol method) — TurnRunner calls it on the raw AppUI.
        # Clear the dangling live indicator (stop the timer/plane) FIRST, then
        # append the aborted marker. _add_renderable closes the open response
        # first, so any partial streamed text stays visible — finalized — with
        # the marker line below it. The partial assistant reply is never recorded
        # in history; that discard is the runtime's (run_turn lets
        # GenerationCancelled propagate before the record site). This is purely
        # display: mark what the user already saw as stopped.
        self._indicator = None
        # `⏹` is not in the Glyphs set; gate it on the same unicode/ascii signal
        # build_app uses, falling back to the ASCII cross glyph.
        marker = "⏹" if self._glyphs == UNICODE_GLYPHS else self._glyphs.cross
        self._add_renderable(Text(f"{marker} aborted", style="sp.warn"))

    def fail_turn(self, message: str) -> None:
        # A turn RAISED (e.g. a network/API error), NOT a clean user Ctrl-C: tear
        # down the dangling live indicator (stop the timer/plane) and surface the
        # error. No "aborted" marker — that's reserved for a user cancel
        # (abort_turn); this is a failure. show_error → _add_renderable closes the
        # open response first, so any partial streamed text stays visible above
        # the error line. Without this the indicator dangled after a turn error.
        self._indicator = None
        self.show_error(message)

    def toggle_at(self, line: int) -> bool:
        # Map a pane click (its document row) to the trail/diff whose transcript
        # line range contains it and flip that element's collapse state (§31.16/
        # §31.19). Clicking reaches ANY element — including older ones scrolled
        # back — which a single "latest" keybinding cannot. Returns False when the
        # click landed outside every toggleable element, so the caller lets the
        # default mouse handling (e.g. scroll) run.
        for start, end, element in self._toggle_ranges:
            if start <= line < end:
                element.expanded = not element.expanded
                self._cache = None
                return True
        return False

    def toggle_latest_diff(self) -> bool:
        # Flip the most-recent diff's collapse state — the Ctrl-O keyboard fallback
        # for terminals without mouse reporting (§31.16). Returns False (a harmless
        # no-op) when no diff exists yet.
        if self._latest_diff is None:
            return False
        self._latest_diff.expanded = not self._latest_diff.expanded
        self._cache = None
        return True

    def stream_thinking(self, text: str) -> None:
        # The reasoning count climbs ONLY while the model is thinking, so it freezes
        # naturally when thinking stops and resumes if it thinks again. A no-op when
        # no turn is active (defensive; begin_response runs first in the tool loop).
        if self._indicator is None:
            return  # no active turn — nothing to attribute the thinking to
        self._indicator.reasoning_chars += len(text)
        self._cache = None
        # Inline trail (§31.19): retain the thinking TEXT for display only (never fed
        # back to the model). Gated on show_reasoning — when off, no trail is built
        # (the readout is hidden too). A new reasoning phase (no active trail) opens a
        # fresh trail block at the current transcript position; close any open
        # response first so the block lands after streamed answer text.
        if not self._show_reasoning:
            return
        if self._active_trail is None:
            # First reasoning fragment of this model call → open a fresh trail at the
            # current transcript position. Do NOT close the open response: a trailing
            # thought after the answer started joins the call's reasoning, it does not
            # cut the answer (§31.19). The trail resets at end_response.
            trail = _Trail()
            self._renderables.append(trail)
            self._active_trail = trail
        self._active_trail.text += text
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
        self._idle_hint_shown = False  # a new turn re-arms the idle hint
        # Breathing room between turns (§31.12): a blank spacer above the echo
        # keeps consecutive turns from blurring together. The first message
        # into an empty pane (or over only an open response) skips it.
        if self._renderables:
            self._add_renderable(Text(""))
        # Brightness hierarchy: the chevron carries the accent, the user's own
        # words render bright — not tinted green like harness machinery.
        echo = Text.assemble(
            (f"{self._glyphs.chevron} ", "sp.chevron"),
            (_sanitize_line(text), "sp.emph"),
        )
        self._add_renderable(echo)

    def show_idle_hint(self, text: str) -> None:
        # An idle Ctrl-C hint (§31.17). Deduped: repeated Ctrl-C while idle shows
        # it once, not a growing stack — the flag re-arms only at the next turn
        # (show_user_message) or a /clear. The text is sanitized like any status.
        if self._idle_hint_shown:
            return
        self._idle_hint_shown = True
        self.show_status(text)

    def show_slash_output(self, text: str) -> None:
        # Slash output rendered by the dispatcher's capturing console (ANSI)
        # becomes a pane renderable (§31.17). Text.from_ansi parses the ANSI
        # styling back into a Rich Text so the pane re-emits it. _add_renderable
        # closes any open response first, so the slash block lands after it.
        # NOTE: captured at the call-time width; a later resize will not re-wrap
        # this block (acceptable for slash output).
        stripped = text.rstrip("\n")
        if stripped:
            self._add_renderable(Text.from_ansi(stripped))

    def clear_conversation(self, message: str | None = None) -> None:
        """Reset the visible pane after the runtime history has been cleared."""
        self._renderables.clear()
        self._open_response = None
        self._indicator = None
        self._active_trail = None
        self._latest_diff = None
        self._toggle_ranges = []
        self._cache = None
        self._idle_hint_shown = False  # a cleared pane re-arms the idle hint
        if message:
            self.show_status(message)

    def show_status(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.dim"))

    def show_choices(self, choices: Text) -> None:
        # The styled approval/plan choice line (colored y/e/n) — already built by
        # render.approval_choices / plan_choices from hardcoded tokens (no user or
        # model content), so it carries its own styling and needs no sanitization.
        self._add_renderable(choices)

    def show_error(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.error"))

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        # Redact secrets so an auto-approved tool call never exposes credentials
        # in the visible pane channel. tool_call_block frames the actual
        # command/url (run_command, web_fetch) or shows a clean inline subject
        # (paths/queries) instead of the name(args) repr (§31.3); a `path` subject
        # is the resolved, workspace-relative target so it can't be spoofed and
        # matches the file actually touched (§14.5). A framed tool call appends
        # two renderables (header + box); _add_renderable closes the open response
        # on the first, keeping the response→tool-call ordering intact.
        redacted = redact_structure(arguments)
        assert isinstance(redacted, dict)
        for renderable in tool_call_block(
            name, redacted, self._glyphs, path_display=self._path_display
        ):
            self._add_renderable(renderable)

    def _path_display(self, path: str) -> str:
        # Resolve a `path` argument to its workspace-relative target (§14.5).
        # Prefer the live workspace (workspace_fn, set in production) so a
        # mid-session /cwd is honoured; fall back to the build-time workspace,
        # then verbatim (a test-double with neither set — production always wires
        # workspace_fn, so the path display never drifts from the action).
        workspace = self._workspace_fn() if self._workspace_fn is not None else self._workspace
        return workspace_display(workspace, path) if workspace is not None else path

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._add_renderable(render_tool_result(success, summary, self._glyphs))

    def show_command_output(self, line: str) -> None:
        # "    " prefix + control-char sanitization + dim, no markup, no highlight —
        # mirroring TerminalUI (markup=False/highlight=False are implied by Text).
        self._add_renderable(Text("    " + _sanitize_line(line), style="sp.dim"))

    def show_plan_progress(self, plan: TaskPlan) -> None:
        # Uses plan_step_line (not plan_panel) with 2-cell left indent, then a blank
        # line — mirroring TerminalUI.show_plan_progress exactly. This is the one
        # content-appender that bypasses _add_renderable, so it finalizes the active
        # trail itself — keeping the §31.19 invariant (any non-thinking content ends
        # the reasoning phase) uniformly true rather than relying on the caller.
        self._finalize_active_trail()
        self._close_open_response()
        for index, step in enumerate(plan.steps, 1):
            self._renderables.append(
                Padding(plan_step_line(index, step, self._glyphs), (0, 0, 0, 2))
            )
        self._renderables.append(Text(""))  # blank line separator
        self._cache = None

    # ------------------------------------------------------------------
    # Approval prompt content (design section 31.16). The blocking handshake
    # lives in ApprovalGate; these only append the prompt block to the pane,
    # mirroring the render block of TerminalUI.ask_approval / ask_plan_approval
    # MINUS the DiffReveal animation — the pane scroll replaces the Rich Live.
    # ------------------------------------------------------------------

    def show_approval(self, request: ApprovalRequest) -> None:
        if request.diff:
            # A stateful, click/Ctrl-O-collapsible diff (§31.16) — rendered at the
            # pane width by _render_diff each frame, so it tracks resizes and its
            # collapse toggle. total_rows is captured once here (not per frame).
            self._close_open_response()
            diff = _Diff(request.diff, total_rows=diff_row_count(request.diff, self._glyphs))
            self._renderables.append(diff)
            self._latest_diff = diff
            self._cache = None
        self._add_renderable(approval_info(request, plain_badge=False))
        self._add_renderable(approval_cwd(request))

    def show_plan_approval(self, plan: TaskPlan, path: str) -> None:
        self._add_renderable(plan_panel(plan, self._glyphs))
        # The path is user-visible state reaching the pane → sanitize it.
        self._add_renderable(Text(_sanitize_line(path), style="sp.faint"))
