"""Tests for live markdown streaming and the aviation spinner (section 31.7/31.8)."""

from __future__ import annotations

import difflib
import io
import random
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

import shellpilot.cli.streaming as streaming_mod
from shellpilot.cli.streaming import (
    FLIGHT_PHASES,
    AviationSpinner,
    DiffReveal,
    ResponseStream,
    phase_for_elapsed,
)
from shellpilot.cli.theme import ASCII_GLYPHS, SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.config.loader import load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI

GLYPHS = UNICODE_GLYPHS

# All phrases across all pools — used to assert labeled frames contain none of them.
ALL_PHRASES = {p for phase in FLIGHT_PHASES for p in phase.pool}


def plain_console() -> Console:
    return Console(record=True, file=io.StringIO(), theme=SHELLPILOT_THEME)


def terminal_console() -> Console:
    return Console(
        record=True,
        file=io.StringIO(),
        theme=SHELLPILOT_THEME,
        force_terminal=True,
        width=80,
    )


def test_response_stream_plain_passthrough() -> None:
    console = plain_console()
    stream = ResponseStream(console)
    for token in ("Hello", " ", "world"):
        stream.feed(token)
    stream.finish()
    assert "Hello world" in console.export_text()


def test_response_stream_renders_markdown_on_terminals() -> None:
    console = terminal_console()
    stream = ResponseStream(console)
    for token in ("Two ", "**bold** words ", "and `code`."):
        stream.feed(token)
    stream.finish()
    out = console.export_text()
    assert "bold" in out and "code" in out
    assert "**bold**" not in out  # markdown was rendered, not echoed


def test_response_stream_sanitizes_every_markdown_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources: list[str] = []
    real_markdown = streaming_mod.Markdown

    def recording_markdown(markup: str) -> Markdown:
        sources.append(markup)
        return real_markdown(markup)

    monkeypatch.setattr(streaming_mod, "Markdown", recording_markdown)
    console = terminal_console()
    stream = ResponseStream(console)
    stream.feed("alpha\x1b[2J\x00\n\n**bold**\x07")
    stream.finish()

    assert len(sources) >= 2
    assert all(not any(char in source for char in "\x1b\x00\x07") for source in sources)
    assert "alpha" in sources[-1]
    assert "\n\n**bold**" in sources[-1]


def test_response_stream_sanitizes_plain_passthrough_and_buffer() -> None:
    console = plain_console()
    stream = ResponseStream(console)
    stream.feed("plain\x1b[2J\x00\ttext\x7f\n")

    out = console.export_text()
    assert not any(char in out for char in "\x1b\x00\x7f\t")
    assert not any(char in stream._buffer for char in "\x1b\x00\x7f\t")
    assert "plain" in out and "text" in out
    assert out.endswith("\n")


def test_response_stream_preserves_multiline_fenced_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 sanitization must not corrupt legitimate multi-line fenced markdown.

    Asserts on the markup handed to Markdown — not the Live-streamed export_text,
    which records intermediate frames and so cannot equal a single static print
    (every other terminal test here uses substring checks for the same reason).
    """
    sources: list[str] = []
    real_markdown = streaming_mod.Markdown

    def recording_markdown(markup: str) -> Markdown:
        sources.append(markup)
        return real_markdown(markup)

    monkeypatch.setattr(streaming_mod, "Markdown", recording_markdown)
    content = 'Intro paragraph.\n\n```python\nprint("hello")\n```\n\nFinal **bold** line.\n'
    console = terminal_console()
    stream = ResponseStream(console)
    for token in (content[:13], content[13:31], content[31:]):
        stream.feed(token)
    stream.finish()

    # The final flush hands the complete, un-corrupted markdown to Markdown:
    # the fenced code block and surrounding text survive sanitization intact.
    assert sources[-1] == content
    out = console.export_text()
    assert 'print("hello")' in out  # code-block body rendered
    assert "Final" in out and "bold" in out  # trailing text + bold rendered


def test_response_stream_final_render_is_complete() -> None:
    console = terminal_console()
    stream = ResponseStream(console)
    for index in range(100):
        stream.feed(f"line {index}\n\n")
    stream.finish()
    out = console.export_text()
    assert "line 0" in out and "line 99" in out


def test_response_stream_finish_without_tokens_is_silent() -> None:
    console = terminal_console()
    ResponseStream(console).finish()
    assert console.export_text() == ""


def test_spinner_noop_without_terminal() -> None:
    spinner = AviationSpinner(plain_console(), GLYPHS, enabled=True)
    spinner.start()
    assert not spinner.active
    spinner.stop()


def test_spinner_starts_and_stops_idempotently() -> None:
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=True)
    spinner.start()
    assert spinner.active
    spinner.stop()
    assert not spinner.active
    spinner.stop()  # second stop must be safe


def test_spinner_disabled_by_setting() -> None:
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False)
    spinner.start()
    assert not spinner.active


# ---------------------------------------------------------------------------
# phase_for_elapsed tests (replaces test_verb_progression)
# ---------------------------------------------------------------------------


def test_phase_for_elapsed_boundaries() -> None:
    """Phase selection: ground 0-<10, climb 10-<20, cruise 20-<60, long-haul 60+."""
    assert phase_for_elapsed(0).name == "ground"
    assert phase_for_elapsed(9.9).name == "ground"
    assert phase_for_elapsed(10.0).name == "climb"
    assert phase_for_elapsed(19.9).name == "climb"
    assert phase_for_elapsed(20.0).name == "cruise"
    assert phase_for_elapsed(59.9).name == "cruise"
    assert phase_for_elapsed(60.0).name == "long-haul"
    assert phase_for_elapsed(1000.0).name == "long-haul"


def test_phase_for_elapsed_is_deterministic() -> None:
    """phase_for_elapsed is a pure function — same input always returns same phase."""
    for elapsed in (0.0, 5.0, 10.0, 20.0, 60.0, 120.0):
        assert phase_for_elapsed(elapsed).name == phase_for_elapsed(elapsed).name


def test_phase_pools_are_populated() -> None:
    """Each phase pool must be non-empty and contain only lowercase strings."""
    for phase in FLIGHT_PHASES:
        assert len(phase.pool) > 0, f"phase {phase.name} has an empty pool"
        for phrase in phase.pool:
            assert phrase == phrase.lower(), f"phrase {phrase!r} is not lowercase"


# ---------------------------------------------------------------------------
# Seeded-determinism and rotation tests (no sleeps)
# ---------------------------------------------------------------------------


def test_seeded_determinism() -> None:
    """Two spinners built with the same seed produce identical phrase sequences."""
    spinner_a = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(42))
    spinner_b = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(42))

    sequence_a = [spinner_a._phrase_for(e) for e in (0.0, 1.0, 5.0, 10.0, 20.0, 60.0)]
    # Reset state for b — simulate a fresh start by resetting phrase state.
    sequence_b = [spinner_b._phrase_for(e) for e in (0.0, 1.0, 5.0, 10.0, 20.0, 60.0)]
    assert sequence_a == sequence_b


def test_rotation_cadence() -> None:
    """Same phrase within a window; new phrase at the boundary."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(1))
    # Reset state as if start() was called.
    spinner._phrase = None
    spinner._next_rotate = 0.0

    p0 = spinner._phrase_for(0.0)
    p1 = spinner._phrase_for(1.0)
    p5 = spinner._phrase_for(5.0)
    p9 = spinner._phrase_for(9.9)
    # All should be the same phrase (within-window, _PHRASE_SECONDS=10).
    assert p0 == p1 == p5 == p9

    # At elapsed == _PHRASE_SECONDS a rotation must occur (or the boundary may be >).
    p10 = spinner._phrase_for(10.0)
    # p10 must be from climb pool and may differ.
    assert p10 in phase_for_elapsed(10.0).pool

    p15 = spinner._phrase_for(15.0)
    # Should still be same as p10 (no new rotation before 20 s).
    assert p15 == p10

    p20 = spinner._phrase_for(20.0)
    assert p20 in phase_for_elapsed(20.0).pool


def test_no_immediate_repeat() -> None:
    """Successive rotations within one phase never yield the same phrase twice in a row."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(99))
    spinner._phrase = None
    spinner._next_rotate = 0.0

    # Force many rotations within ground phase (start < 10).
    # Artificially reset next_rotate each iteration to trigger a pick.
    prev: str | None = None
    for _ in range(30):
        spinner._next_rotate = 0.0  # force rotation
        phrase = spinner._phrase_for(0.0)
        if prev is not None:
            assert phrase != prev, f"Immediate repeat: {phrase!r}"
        prev = phrase


def test_phase_progression_never_regresses() -> None:
    """Phrases at 65 s come from long-haul pool, not ground or climb."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(7))
    spinner._phrase = None
    spinner._next_rotate = 0.0

    phrase = spinner._phrase_for(65.0)
    assert phrase in phase_for_elapsed(65.0).pool
    ground_pool = phase_for_elapsed(0.0).pool
    assert phrase not in ground_pool or phrase in phase_for_elapsed(65.0).pool


# ---------------------------------------------------------------------------
# Frame-rendering tests (labeled vs unlabeled)
# ---------------------------------------------------------------------------


def test_unlabeled_frame_uses_spinner_frames_glyph() -> None:
    """Unlabeled mode: first span styled sp.accent, glyph from spinner_frames."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(0))
    spinner._started_at = 0.0
    spinner._phrase = None
    spinner._next_rotate = 0.0
    frame = spinner._frame(0)
    assert isinstance(frame, Text)
    # First span must be in accent style.
    first_span = frame._spans[0] if frame._spans else None
    assert first_span is not None
    first_text = frame.plain[first_span.start : first_span.end]
    assert first_text in GLYPHS.spinner_frames


def test_labeled_frame_uses_beacon_frames_glyph() -> None:
    """Labeled mode: glyph from beacon_frames, first span in sp.accent."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(0))
    spinner._started_at = 0.0
    spinner._label = "fueling gemma4:e4b"
    frame = spinner._frame(0)
    assert isinstance(frame, Text)
    first_span = frame._spans[0] if frame._spans else None
    assert first_span is not None
    first_text = frame.plain[first_span.start : first_span.end]
    assert first_text in GLYPHS.beacon_frames


def test_labeled_frame_contains_no_flight_phrase() -> None:
    """When started with a label, frames show the label text and NO flight-phase phrase."""
    import time

    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=True)
    spinner.start(label="fueling gemma4:e4b")
    time.sleep(0.2)
    assert spinner.active
    frame_text = spinner._current_label_text()
    assert "fueling gemma4:e4b" in frame_text
    for phrase in ALL_PHRASES:
        assert phrase not in frame_text, f"unexpected phrase {phrase!r} in labeled frame"
    spinner.stop()
    assert not spinner.active


def test_unlabeled_frame_uses_flight_phrase() -> None:
    """When start() is called without a label, the flight phrases are used."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=True)
    spinner.start()
    assert spinner.active
    frame_text = spinner._current_label_text()
    assert any(phrase in frame_text for phrase in ALL_PHRASES)
    spinner.stop()


def test_label_styling_preserved() -> None:
    """A rich Text label containing sp.emph spans keeps them in the rendered frame."""
    label = Text.assemble(("running ", "sp.dim"), ("myTool", "sp.emph"))
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=False, rng=random.Random(0))
    spinner._started_at = 0.0
    spinner._label = label
    frame = spinner._frame(0)
    # The frame must contain the sp.emph span (myTool) somewhere.
    emph_found = any(frame.plain[s.start : s.end] == "myTool" for s in frame._spans)
    assert emph_found, "sp.emph span from label was lost (flattened to plain)"


# ---------------------------------------------------------------------------
# Width-stability tests
# ---------------------------------------------------------------------------


def test_spinner_frames_uniform_width() -> None:
    """All frames in spinner_frames have identical cell width."""
    for glyphs in (UNICODE_GLYPHS, ASCII_GLYPHS):
        widths = [cell_len(f) for f in glyphs.spinner_frames]
        assert len(set(widths)) == 1, (
            f"{glyphs.__class__.__name__} spinner_frames widths differ: {widths}"
        )


def test_beacon_frames_uniform_width() -> None:
    """All frames in beacon_frames have identical cell width."""
    for glyphs in (UNICODE_GLYPHS, ASCII_GLYPHS):
        widths = [cell_len(f) for f in glyphs.beacon_frames]
        assert len(set(widths)) == 1, f"beacon_frames widths differ: {widths}"


# ---------------------------------------------------------------------------
# Commit-1 regression test
# ---------------------------------------------------------------------------


def test_finish_clears_live_before_stop() -> None:
    """finish() must call live.update('', refresh=False) immediately before live.stop().

    This prevents rich Live.stop()'s forced vertical_overflow='visible' repaint from
    leaking content into scrollback on short terminals.
    """
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class SpyLive:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            calls.append(("start", (), {}))

        def update(self, renderable: object, *, refresh: bool = True) -> None:
            calls.append(("update", (renderable,), {"refresh": refresh}))

        def stop(self) -> None:
            calls.append(("stop", (), {}))

    import shellpilot.cli.streaming as streaming_mod

    original_live = streaming_mod.Live
    streaming_mod.Live = SpyLive  # type: ignore[attr-defined]
    try:
        console = terminal_console()
        stream = ResponseStream(console)
        for token in ("alpha ", "beta ", "gamma"):
            stream.feed(token)
        stream.finish()
    finally:
        streaming_mod.Live = original_live  # type: ignore[attr-defined]

    # Find the last update call and the stop call.
    update_calls = [c for c in calls if c[0] == "update"]
    stop_index = next(i for i, c in enumerate(calls) if c[0] == "stop")
    last_update_index = max(i for i, c in enumerate(calls) if c[0] == "update")

    # The last update must immediately precede stop.
    assert last_update_index == stop_index - 1, (
        "expected the last update() to immediately precede stop()"
    )
    last_update = update_calls[-1]
    renderable = last_update[1][0]
    refresh = last_update[2]["refresh"]
    assert renderable == "", f"expected empty string renderable, got {renderable!r}"
    assert refresh is False, "expected refresh=False on the clearing update"


# ---------------------------------------------------------------------------
# DiffReveal: approval-time scrolling reveal (design section 31.4)
# ---------------------------------------------------------------------------


def _additions_diff(count: int, name: str = "big.py") -> str:
    """A unified diff that adds *count* numbered lines to an empty file."""
    before = ""
    after = "".join(f"line {i}\n" for i in range(count))
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


class _SpyLive:
    """Records start/update/stop so reveal ordering can be asserted with no sleeps."""

    instances: list[_SpyLive] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        _SpyLive.instances.append(self)

    def start(self) -> None:
        self.calls.append(("start", (), {}))

    def update(self, renderable: object, *, refresh: bool = True) -> None:
        self.calls.append(("update", (renderable,), {"refresh": refresh}))

    def stop(self) -> None:
        self.calls.append(("stop", (), {}))


def test_diff_reveal_short_diff_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _SpyLive.instances = []
    monkeypatch.setattr(streaming_mod, "Live", _SpyLive)
    reveal = DiffReveal(terminal_console(), GLYPHS, enabled=True)
    # 5 additions = 5 rendered rows, well under ANIMATE_THRESHOLD.
    reveal.reveal(_additions_diff(5), max_rows=DiffReveal.WINDOW_ROWS)
    assert _SpyLive.instances == []  # no Live opened for a short diff


def test_diff_reveal_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _SpyLive.instances = []
    monkeypatch.setattr(streaming_mod, "Live", _SpyLive)
    reveal = DiffReveal(terminal_console(), GLYPHS, enabled=False)
    reveal.reveal(_additions_diff(40), max_rows=DiffReveal.WINDOW_ROWS)
    assert _SpyLive.instances == []  # motion off → no Live even for a long diff


def test_diff_reveal_nontty_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _SpyLive.instances = []
    monkeypatch.setattr(streaming_mod, "Live", _SpyLive)
    # plain_console() is not a terminal → enabled collapses to False internally.
    reveal = DiffReveal(plain_console(), GLYPHS, enabled=True)
    reveal.reveal(_additions_diff(40), max_rows=DiffReveal.WINDOW_ROWS)
    assert _SpyLive.instances == []


def test_diff_reveal_long_diff_predicate_is_independent_of_enabled() -> None:
    # reveal() returns True for a long diff even when animation is disabled (non-TTY),
    # so the settled panel still applies the WINDOW_ROWS cap on non-interactive output.
    reveal = DiffReveal(plain_console(), GLYPHS, enabled=True)
    assert reveal.reveal(_additions_diff(30), max_rows=DiffReveal.WINDOW_ROWS) is True
    assert reveal.reveal(_additions_diff(5), max_rows=DiffReveal.WINDOW_ROWS) is False


def test_diff_reveal_long_diff_animates_and_clears_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SpyLive.instances = []
    monkeypatch.setattr(streaming_mod, "Live", _SpyLive)
    monkeypatch.setattr(streaming_mod.time, "sleep", lambda _seconds: None)  # no real delay
    reveal = DiffReveal(terminal_console(), GLYPHS, enabled=True)
    reveal.reveal(_additions_diff(40), max_rows=DiffReveal.WINDOW_ROWS)

    assert len(_SpyLive.instances) == 1
    live = _SpyLive.instances[0]
    names = [c[0] for c in live.calls]
    assert names[0] == "start"
    assert names[-1] == "stop"
    # The clearing update must immediately precede stop (mirrors finish()).
    stop_index = names.index("stop")
    last_update_index = max(i for i, n in enumerate(names) if n == "update")
    assert last_update_index == stop_index - 1
    clearing = live.calls[last_update_index]
    assert clearing[1][0] == ""
    assert clearing[2]["refresh"] is False


def test_diff_reveal_chunk_math_bounds_tick_count() -> None:
    """The chunk size keeps any diff length within TOTAL_DURATION's tick budget."""
    import math

    ticks_budget = math.floor(DiffReveal.TOTAL_DURATION / streaming_mod._REFRESH_SECONDS)
    for total in (21, 40, 60, 200, 500):
        chunk = max(1, math.ceil(total / max(1, ticks_budget)))
        frames = math.ceil(total / chunk)
        assert frames <= ticks_budget, f"{total} rows took {frames} frames > {ticks_budget}"


def test_runtime_emits_response_hooks_and_turn_stats(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    ui = FakeUI()
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[answer("hello there")]),
        settings=loaded.settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )
    runtime.run_turn("hi")
    assert ui.began == 1
    assert ui.ended == 1
    assert len(ui.turn_stats) == 1
    stats = ui.turn_stats[0]
    assert stats.elapsed_s >= 0
    assert stats.context_tokens > 0
    assert 0 <= stats.context_pct <= 100
    assert stats.warn is False
