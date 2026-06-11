"""Live markdown streaming and the aviation spinner (design sections 31.7, 31.8).

ResponseStream renders the in-progress response as markdown inside a transient
Live region (showing the tail when the response outgrows the screen), then
prints exactly one clean final render — scrollback always ends with one perfect
copy. AviationSpinner fills the wait before the first token and always erases
itself, including on Ctrl-C.
"""

from __future__ import annotations

import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from shellpilot.cli.theme import Glyphs

SPINNER_VERBS = ("taxiing", "climbing", "cruising", "on approach")
_VERB_SECONDS = 4
_REFRESH_SECONDS = 0.08


def verb_for_elapsed(elapsed_s: float) -> str:
    index = min(int(elapsed_s) // _VERB_SECONDS, len(SPINNER_VERBS) - 1)
    return SPINNER_VERBS[index]


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
        return Markdown(tail)

    def feed(self, token: str) -> None:
        if not self._console.is_terminal:
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
            self._live.stop()
            self._live = None
        if self._buffer:
            self._console.print(Markdown(self._buffer))
        self._buffer = ""


class AviationSpinner:
    """Dim flight-phase status while the model works; erases itself cleanly."""

    def __init__(self, console: Console, glyphs: Glyphs, *, enabled: bool) -> None:
        self._console = console
        self._glyphs = glyphs
        self._enabled = enabled and console.is_terminal
        self._live: Live | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = 0.0

    @property
    def active(self) -> bool:
        return self._live is not None

    def _frame(self, tick: int) -> Text:
        frames = self._glyphs.spinner_frames
        elapsed = time.monotonic() - self._started_at
        verb = verb_for_elapsed(elapsed)
        return Text(
            f"{frames[tick % len(frames)]} {verb}{self._glyphs.ellipsis} {int(elapsed)}s",
            style="sp.dim",
        )

    def _spin(self) -> None:
        tick = 0
        while not self._stop_event.wait(_REFRESH_SECONDS):
            live = self._live
            if live is None:
                return
            tick += 1
            live.update(self._frame(tick), refresh=True)

    def start(self) -> None:
        if not self._enabled or self._live is not None:
            return
        self._started_at = time.monotonic()
        self._stop_event.clear()
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
