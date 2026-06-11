"""Tests for live markdown streaming and the aviation spinner (section 31.7/31.8)."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from shellpilot.cli.streaming import (
    AviationSpinner,
    ResponseStream,
    verb_for_elapsed,
)
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.config.loader import load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI

GLYPHS = UNICODE_GLYPHS


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


def test_verb_progression() -> None:
    assert verb_for_elapsed(0) == "taxiing"
    assert verb_for_elapsed(5) == "climbing"
    assert verb_for_elapsed(9) == "cruising"
    assert verb_for_elapsed(60) == "on approach"


# ---------------------------------------------------------------------------
# A10: labeled spinner tests
# ---------------------------------------------------------------------------


def test_spinner_label_overrides_verbs() -> None:
    """When start() is called with a label, frames show the label not flight verbs."""
    import time

    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=True)
    spinner.start(label="fueling gemma4:e4b")
    # Give the spin thread one tick to produce an updated frame.
    time.sleep(0.2)
    # Grab the current frame text from the live renderable.
    assert spinner.active
    frame_text = spinner._current_label_text()
    assert "fueling gemma4:e4b" in frame_text
    assert "taxiing" not in frame_text
    spinner.stop()
    assert not spinner.active


def test_spinner_label_none_uses_flight_verbs() -> None:
    """When start() is called without a label, the flight verbs are used as before."""
    spinner = AviationSpinner(terminal_console(), GLYPHS, enabled=True)
    spinner.start()
    assert spinner.active
    frame_text = spinner._current_label_text()
    # Flight verbs are used when no label is set.
    assert any(v in frame_text for v in ("taxiing", "climbing", "cruising", "on approach"))
    spinner.stop()


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
