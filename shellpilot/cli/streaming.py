"""Live markdown streaming and the aviation spinner (design sections 31.7, 31.8).

ResponseStream renders the in-progress response as markdown inside a transient
Live region (showing the tail when the response outgrows the screen), then
prints exactly one clean final render — scrollback always ends with one perfect
copy, including when the response is taller than the terminal. AviationSpinner
fills the wait before the first token and always erases itself, including on
Ctrl-C.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import ClassVar, NamedTuple

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from shellpilot.cli.render import _diff_rows, _sanitize_line
from shellpilot.cli.theme import Glyphs

_PHRASE_SECONDS = 10
_REFRESH_SECONDS = 0.08


class FlightPhase(NamedTuple):
    """One ordered phase in the spinner's flight narrative."""

    name: str
    start: float
    pool: tuple[str, ...]


# Four ordered flight phases.  The spinner progresses through them as elapsed
# time grows — never regresses.  No approach/landing phrases are included
# because the spinner cannot know when the model will finish.
FLIGHT_PHASES: tuple[FlightPhase, ...] = (
    FlightPhase(
        name="ground",
        start=0.0,
        pool=(
            "taxiing",
            "running the checklist",
            "requesting clearance",
            "pushing back",
            "spooling up",
            "holding short",
            "lining up",
            "chocks away",
            "final walkaround",
            "cleared for takeoff",
        ),
    ),
    FlightPhase(
        name="climb",
        start=10.0,
        pool=(
            "climbing",
            "wheels up",
            "rotating",
            "gear up",
            "full throttle",
            "climbing through clouds",
            "departing the pattern",
            "trimming the climb",
            "on the upwind leg",
        ),
    ),
    FlightPhase(
        name="cruise",
        start=20.0,
        pool=(
            "cruising",
            "on autopilot",
            "scanning the instruments",
            "riding the jetstream",
            "above the weather",
            "steady at altitude",
            "crossing waypoints",
            "following the flight plan",
            "smooth air ahead",
            "trimmed for level flight",
        ),
    ),
    FlightPhase(
        name="long-haul",
        start=60.0,
        pool=(
            "holding pattern",
            "circling the field",
            "crossing time zones",
            "chasing the horizon",
            "stretching the glide",
            "awaiting vectors",
            "counting runway lights",
            "long-haul leg",
        ),
    ),
)


def phase_for_elapsed(elapsed: float) -> FlightPhase:
    """Return the current FlightPhase for *elapsed* seconds.

    Deterministic: the last phase whose ``start <= elapsed``.  Never regresses.
    """
    phase = FLIGHT_PHASES[0]
    for p in FLIGHT_PHASES:
        if elapsed >= p.start:
            phase = p
    return phase


class ResponseStream:
    """Accumulates streamed tokens; live markdown on terminals, passthrough otherwise."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._buffer = ""
        self._live: Live | None = None
        self._last_refresh = 0.0

    def _tail_markdown(self) -> Markdown:
        max_lines = max(4, self._console.size.height - 4)
        tail = "\n".join(self._buffer.splitlines()[-max_lines:])
        return Markdown(_sanitize_line(tail))

    def feed(self, token: str) -> None:
        if not self._console.is_terminal:
            token = _sanitize_line(token)
            self._console.print(token, end="", markup=False, highlight=False, soft_wrap=True)
            self._buffer += token
            return
        self._buffer += token
        if self._live is None:
            self._live = Live(
                self._tail_markdown(),
                console=self._console,
                transient=True,
                auto_refresh=False,
                vertical_overflow="crop",
            )
            self._live.start()
        now = time.monotonic()
        if now - self._last_refresh >= _REFRESH_SECONDS:
            self._live.update(self._tail_markdown(), refresh=True)
            self._last_refresh = now

    def finish(self) -> None:
        """Stop the live region and leave one clean copy of the full response."""
        if not self._console.is_terminal:
            if self._buffer:
                self._console.print()
            self._buffer = ""
            return
        if self._live is not None:
            # Clear the renderable before stopping so Live.stop()'s forced
            # vertical_overflow="visible" repaint renders an empty region. Without this,
            # a tail taller than the terminal pushes lines into scrollback that the
            # transient erase cannot reach, and the final Markdown print below them
            # makes the response appear twice.
            self._live.update("", refresh=False)
            self._live.stop()
            self._live = None
        if self._buffer:
            self._console.print(Markdown(_sanitize_line(self._buffer)))
        self._buffer = ""


class DiffReveal:
    """Transient scrolling reveal for a long approval diff (design section 31.4).

    Synchronous and main-thread (unlike :class:`AviationSpinner`): the approval
    prompt is already waiting on user input, so blocking through a brief reveal
    is safe and avoids a second background Live. ``reveal()`` is a no-op when
    motion is disabled, on a non-terminal, or for a short diff — the caller then
    just prints the settled panel.

    LIVE-ORDERING (load-bearing): two concurrent ``rich.live.Live`` on one
    Console corrupt the display. The caller (``TerminalUI.ask_approval``) MUST
    stop the spinner — ``self._spinner.stop()`` joins its thread and stops its
    Live before returning — strictly BEFORE invoking ``reveal()``, so this Live
    only ever opens after the spinner's is fully gone.
    """

    # A diff shorter than this never animates (a 3-line patch ≈ 6 rows). Constant,
    # not console height: height is unknown/0 in tests and this is a UX heuristic.
    ANIMATE_THRESHOLD: ClassVar[int] = 20
    # Rows visible in the reveal window AND the settled-panel cap. Same value so
    # the last reveal frame equals the settled panel; the footer reports the rest.
    WINDOW_ROWS: ClassVar[int] = 24
    # Total reveal time, bounded regardless of length: a huge diff adds no real
    # latency (chunk size scales with row count to keep within this budget).
    TOTAL_DURATION: ClassVar[float] = 0.6
    # ponytail: tools/patch.py unified_diff already caps the source at
    # MAX_PREVIEW_LINES = 60, so request.diff is <=~60 lines and these row-level
    # bounds are a second display layer — no need to re-cap beyond WINDOW_ROWS.

    def __init__(self, console: Console, glyphs: Glyphs, *, enabled: bool) -> None:
        self._console = console
        self._glyphs = glyphs
        self._enabled = enabled and console.is_terminal

    def row_count(self, diff_text: str) -> int:
        """Number of rendered diff rows — pure, cheap, no Panel built."""
        rows, _ = _diff_rows(diff_text, self._glyphs)
        return len(rows)

    def reveal(self, diff_text: str, *, max_rows: int) -> None:
        """Animate a scrolling reveal of *diff_text*, then return.

        No-op when motion is off, on a non-terminal, or for a diff at or below
        ``ANIMATE_THRESHOLD`` rows. The caller prints the settled (capped) panel
        next regardless — capping is a display choice, not part of the motion.
        """
        rows, _ = _diff_rows(diff_text, self._glyphs)
        if not self._enabled or len(rows) <= self.ANIMATE_THRESHOLD:
            return
        total = len(rows)
        ticks = max(1, math.floor(self.TOTAL_DURATION / _REFRESH_SECONDS))
        chunk = max(1, math.ceil(total / ticks))
        live = Live(
            self._frame(rows, chunk, max_rows),
            console=self._console,
            transient=True,
            auto_refresh=False,
            vertical_overflow="crop",
        )
        live.start()
        try:
            revealed = chunk
            while revealed < total:
                time.sleep(_REFRESH_SECONDS)
                revealed += chunk
                live.update(self._frame(rows, revealed, max_rows), refresh=True)
        finally:
            # Clear before stop so Live.stop()'s forced vertical_overflow="visible"
            # repaint renders nothing — same clear-before-stop as
            # ResponseStream.finish(), preventing a tall reveal from leaking into
            # scrollback and double-rendering under the settled panel.
            live.update("", refresh=False)
            live.stop()

    def _frame(self, rows: list[Text], revealed: int, max_rows: int) -> Group:
        """A reveal frame: the last ``max_rows`` of the rows revealed so far."""
        shown = rows[: min(revealed, len(rows))]
        window = shown[-max_rows:]
        return Group(*window)


class AviationSpinner:
    """Accent-colored flight-phase status while the model works; erases itself cleanly.

    In unlabeled (thinking) mode, a compact-glide plane animates across a 4-cell track
    in ``sp.accent`` while flight-phase phrases cycle every ``_PHRASE_SECONDS`` seconds.
    Phrases are drawn randomly from the current phase's pool (random within an ordered,
    never-regressing phase progression: coherent story within a turn, variety across
    turns, no approach/landing phrases because completion time is unknowable).

    When ``start(label=...)`` is given a label, every frame renders a breathing-beacon
    glyph in ``sp.accent`` followed by the label with its rich styling preserved (no
    longer flattened to plain text).  ``None`` label preserves original behaviour.
    """

    def __init__(
        self,
        console: Console,
        glyphs: Glyphs,
        *,
        enabled: bool,
        rng: random.Random | None = None,
    ) -> None:
        self._console = console
        self._glyphs = glyphs
        self._enabled = enabled and console.is_terminal
        self._live: Live | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = 0.0
        self._label: str | Text | None = None
        # Random phrase rotation state.
        self._rng = rng or random.Random()
        self._phrase: str | None = None
        self._next_rotate: float = 0.0

    @property
    def active(self) -> bool:
        return self._live is not None

    def _phrase_for(self, elapsed: float) -> str:
        """Return the current display phrase, rotating every _PHRASE_SECONDS.

        Single-threaded access: only the spinner thread and the pre-thread
        ``_frame(0)`` call (inside ``start()``) call this method.
        """
        if elapsed >= self._next_rotate:
            phase = phase_for_elapsed(elapsed)
            # Never repeat the current phrase immediately; fall back to full pool
            # only when the pool has a single entry.
            choices = [p for p in phase.pool if p != self._phrase]
            if not choices:
                choices = list(phase.pool)
            self._phrase = self._rng.choice(choices)
            self._next_rotate = elapsed + _PHRASE_SECONDS
        # _phrase is always set after the block above, but the type checker cannot
        # prove it so we provide a safe fallback.
        return self._phrase or phase_for_elapsed(elapsed).pool[0]

    def _frame(self, tick: int) -> Text:
        elapsed = time.monotonic() - self._started_at
        if self._label is not None:
            # Labeled (fueling / running) mode: breathing-beacon glyph in accent,
            # label with its rich styling intact, dim elapsed suffix.
            beacon = self._glyphs.beacon_frames[tick % len(self._glyphs.beacon_frames)]
            suffix = f"{self._glyphs.ellipsis} {int(elapsed)}s"
            base = Text.assemble((beacon, "sp.accent"), (" ", "sp.dim"))
            if isinstance(self._label, Text):
                base.append_text(self._label.copy())
            else:
                base.append(self._label, style="sp.dim")
            base.append(suffix, style="sp.dim")
            return base
        # Unlabeled (thinking) mode: compact-glide plane in accent, phrase + elapsed dim.
        glyph = self._glyphs.spinner_frames[tick % len(self._glyphs.spinner_frames)]
        phrase = self._phrase_for(elapsed)
        return Text.assemble(
            (glyph, "sp.accent"),
            (f" {phrase}", "sp.dim"),
            (f"{self._glyphs.ellipsis} {int(elapsed)}s", "sp.dim"),
        )

    def _current_label_text(self) -> str:
        """Return the plain text of the most recent frame (for tests)."""
        return self._frame(0).plain

    def _spin(self) -> None:
        tick = 0
        while not self._stop_event.wait(_REFRESH_SECONDS):
            live = self._live
            if live is None:
                return
            tick += 1
            live.update(self._frame(tick), refresh=True)

    def start(self, label: str | Text | None = None) -> None:
        """Start the spinner.  When *label* is set the frame shows the label
        instead of the flight-phase phrases; ``None`` preserves original behaviour.
        """
        if not self._enabled or self._live is not None:
            return
        self._label = label
        self._started_at = time.monotonic()
        self._stop_event.clear()
        # Reset phrase state so every turn opens fresh in ground phase.
        self._phrase = None
        self._next_rotate = 0.0
        self._live = Live(
            self._frame(0),
            console=self._console,
            transient=True,
            auto_refresh=False,
        )
        self._live.start()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Idempotent and exception-safe; never leaves a stray spinner line."""
        if self._live is None:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._live.stop()
        self._live = None
        self._label = None
