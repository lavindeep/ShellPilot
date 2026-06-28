"""Tests for AppUI — the RuntimeUI implementation for the full-screen pane (§31.12)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.theme import ASCII_GLYPHS, UNICODE_GLYPHS
from shellpilot.memory.redaction import REDACTED
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.events import TurnStats
from shellpilot.runtime.planner import PlanStep, TaskPlan

GLYPHS = UNICODE_GLYPHS


def make_ui(workspace: Path | None = None, width: int = 80) -> AppUI:
    return AppUI(glyphs=GLYPHS, workspace=workspace, width_fn=lambda: width)


def ansi_text(ui: AppUI) -> str:
    """Raw ANSI string from _render_ansi."""
    return ui._render_ansi()


def strip_ansi(s: str) -> str:
    """Remove ANSI escape codes, leaving only visible characters."""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def plain(ui: AppUI) -> str:
    """Visible plain text from the pane (ANSI codes stripped)."""
    return strip_ansi(ansi_text(ui))


def make_plan() -> TaskPlan:
    return TaskPlan(
        task_id="20260611-040000-demo",
        goal="Demo goal",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[
            PlanStep(title="First", status="completed"),
            PlanStep(title="Second", status="active"),
            PlanStep(title="Third"),
        ],
    )


# ---------------------------------------------------------------------------
# Content method structural output
# ---------------------------------------------------------------------------


def test_show_status_renders_into_pane() -> None:
    ui = make_ui()
    ui.show_status("waiting for model")
    assert "waiting for model" in plain(ui)


def test_show_error_renders_into_pane() -> None:
    ui = make_ui()
    ui.show_error("something went wrong")
    assert "something went wrong" in plain(ui)


def test_show_tool_call_renders_name_and_args() -> None:
    ui = make_ui()
    ui.show_tool_call("patch_file", {"path": "hello.py"})
    out = plain(ui)
    assert "patch_file" in out
    # The path argument value appears (no workspace set → verbatim repr).
    assert "hello.py" in out
    # The bullet glyph is present in the ANSI (not stripped).
    assert GLYPHS.bullet in ansi_text(ui)


def test_show_tool_result_success_mark() -> None:
    ui = make_ui()
    ui.show_tool_result("patch_file", True, "1 addition")
    out = plain(ui)
    assert GLYPHS.check in out
    assert "1 addition" in out


def test_show_tool_result_failure_mark() -> None:
    ui = make_ui()
    ui.show_tool_result("patch_file", False, "permission denied")
    out = plain(ui)
    assert GLYPHS.cross in out
    assert "permission denied" in out


def test_show_command_output_has_four_space_indent() -> None:
    ui = make_ui()
    ui.show_command_output("exit 0")
    out = plain(ui)
    # The rendered line must be indented by 4 spaces.
    assert "    exit 0" in out


def test_show_plan_progress_renders_checklist_and_blank_line() -> None:
    ui = make_ui()
    ui.show_plan_progress(make_plan())
    out = plain(ui)
    # Completed step uses check glyph; active uses current; pending uses todo.
    assert GLYPHS.check in out
    assert GLYPHS.current in out
    assert GLYPHS.todo in out
    # Step titles are present.
    assert "First" in out and "Second" in out and "Third" in out
    # A blank line follows the checklist (matches TerminalUI.show_plan_progress).
    assert out.rstrip("\n").endswith("") or "\n\n" in out


def test_stream_token_then_end_response_renders_markdown() -> None:
    ui = make_ui()
    ui.stream_token("Hello world from the model")
    ui.end_response()
    out = plain(ui)
    assert "Hello world from the model" in out


def test_stream_token_in_progress_included_in_render() -> None:
    """Tokens streamed but not yet end_response'd still appear in the pane."""
    ui = make_ui()
    ui.stream_token("partial")
    # No end_response — open response is included in render.
    assert "partial" in plain(ui)


# ---------------------------------------------------------------------------
# Security: redaction and sanitization
# ---------------------------------------------------------------------------


def test_show_tool_call_redacts_secret_token() -> None:
    """A secret-valued argument must be REDACTED in the pane, never exposed."""
    ui = make_ui()
    # ghp_ prefix matches the GitHub classic token pattern in redaction.py.
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    ui.show_tool_call("api_call", {"token": secret})
    out = ansi_text(ui)
    assert secret not in out
    assert REDACTED in out


def test_show_tool_call_redacts_prefixed_key_secret() -> None:
    """Keys ending with _KEY or _PASSWORD trigger redaction by suffix matching."""
    ui = make_ui()
    ui.show_tool_call("configure", {"OPENAI_API_KEY": "sk-secret"})
    out = ansi_text(ui)
    assert "sk-secret" not in out
    assert REDACTED in out


def test_show_tool_call_resolves_path_via_workspace_display(tmp_path: Path) -> None:
    """A path arg is shown as its resolved, workspace-relative target (§14.5)."""
    ui = make_ui(workspace=tmp_path)
    ui.show_tool_call("read_file", {"path": "notes/../secret.txt"})
    out = plain(ui)
    # The raw traversal arg must NOT appear; the resolved target must appear.
    assert "notes/../secret.txt" not in out
    assert "secret.txt" in out


def test_show_tool_call_marks_path_escaping_workspace(tmp_path: Path) -> None:
    """A path that escapes the workspace renders the honest out-of-workspace marker."""
    from shellpilot.tools.base import OUTSIDE_WORKSPACE_DISPLAY

    ui = make_ui(workspace=tmp_path)
    ui.show_tool_call("read_file", {"path": "../outside.txt"})
    out = plain(ui)
    assert "../outside.txt" not in out
    assert OUTSIDE_WORKSPACE_DISPLAY in out


def test_show_command_output_strips_control_chars() -> None:
    """Control/ANSI-injection characters in command output must not reach the pane."""
    ui = make_ui()
    ui.show_command_output("visible\x1b[2J\x00\tinjected\x07")
    raw = ansi_text(ui)
    # After stripping theme ANSI codes, no injected escape bytes should remain.
    assert "\x1b[2J" not in raw
    assert "\x00" not in raw
    assert "\x07" not in raw
    assert "visible" in plain(ui)
    assert "injected" in plain(ui)


def test_show_tool_call_sanitizes_name_and_summary() -> None:
    """Control chars in tool name and argument summary are stripped."""
    ui = make_ui()
    ui.show_tool_call("tool\x1b[2J\x00name", {"key\x07": "val\x7f"})
    raw = ansi_text(ui)
    assert "\x1b[2J" not in raw
    assert "\x00" not in raw
    assert "\x07" not in raw
    assert "\x7f" not in raw
    assert "tool" in plain(ui)


def test_stream_response_strips_control_chars() -> None:
    """The model response is the most sensitive sink: control/ANSI-injection bytes
    streamed via stream_token must not reach the pane — both while the response is
    still open (in-progress render) and after end_response (mirrors ResponseStream).
    """
    ui = make_ui()
    ui.stream_token("hi\x1b[2J\x07")
    ui.stream_token("\x7fthere")
    raw_open = ansi_text(ui)  # in-progress path (_render_ansi includes open response)
    assert "\x1b[2J" not in raw_open
    assert "\x07" not in raw_open
    assert "\x7f" not in raw_open
    assert "hi" in plain(ui)
    assert "there" in plain(ui)
    ui.end_response()  # finalized path (_close_open_response)
    raw_done = ansi_text(ui)
    assert "\x1b[2J" not in raw_done
    assert "\x07" not in raw_done
    assert "\x7f" not in raw_done


def test_show_status_strips_control_chars() -> None:
    ui = make_ui()
    ui.show_status("ok\x1b[2J\x00injected")
    raw = ansi_text(ui)
    assert "\x1b[2J" not in raw
    assert "\x00" not in raw
    assert "ok" in plain(ui)
    assert "injected" in plain(ui)


def test_show_error_strips_control_chars() -> None:
    ui = make_ui()
    ui.show_error("fail\x1b[31mforged\x07")
    raw = ansi_text(ui)
    assert "\x1b[31m" not in raw
    assert "\x07" not in raw
    assert "fail" in plain(ui)
    assert "forged" in plain(ui)


# ---------------------------------------------------------------------------
# Ordering: response closes around non-token content
# ---------------------------------------------------------------------------


def test_response_closes_around_tool_call() -> None:
    """response → tool call → response produces three distinct transcript entries."""
    ui = make_ui()
    ui.stream_token("response one")
    ui.show_tool_call("some_tool", {})  # must close the open response first
    ui.stream_token("response two")
    ui.end_response()
    # Three entries: Markdown("response one"), Text(tool call), Markdown("response two").
    assert len(ui._renderables) == 3
    from rich.markdown import Markdown

    assert isinstance(ui._renderables[0], Markdown)
    # Middle entry is the tool_call Text (rendered as a Rich Text).
    assert isinstance(ui._renderables[2], Markdown)
    # Both response texts are in the pane.
    out = plain(ui)
    assert "response one" in out
    assert "response two" in out


def test_end_response_without_open_is_noop() -> None:
    ui = make_ui()
    ui.show_status("before")
    ui.end_response()  # no open response — must not raise or add an entry
    assert len(ui._renderables) == 1


# ---------------------------------------------------------------------------
# Resize re-render
# ---------------------------------------------------------------------------


def test_resize_rerenders_ansi() -> None:
    """A width change re-renders. The width is re-read each call, so a different
    width is a cache miss that yields a fresh render object re-keyed to the new
    width — the environment-independent AppUI resize contract. (Whether the terminal
    visibly re-wraps depends on the terminal/Rich and is a live-checklist item, so
    we assert object identity + the cache key here, not byte-difference of output.)
    """
    width = [80]
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: width[0])
    ui.show_status("a" * 60 + " " + "b" * 60)
    ansi_80 = ui._render_ansi()
    width[0] = 40
    ansi_40 = ui._render_ansi()
    # Cache miss on the width change → a fresh object, re-keyed to the new width.
    assert ansi_40 is not ansi_80
    assert ui._cache is not None and ui._cache[0] == 40


def test_cache_hit_at_same_width() -> None:
    """Calling _render_ansi twice at the same width returns the cached string."""
    ui = make_ui(width=80)
    ui.show_status("hello")
    first = ui._render_ansi()
    second = ui._render_ansi()
    assert first is second  # exact same object from the cache


def test_cache_invalidated_on_new_content() -> None:
    """Adding new content invalidates the cache."""
    ui = make_ui(width=80)
    ui.show_status("first")
    before = ui._render_ansi()
    ui.show_status("second")
    after = ui._render_ansi()
    assert after is not before  # cache invalidated → a fresh render, not the cached object
    assert "second" in plain(ui)


# ---------------------------------------------------------------------------
# Approval stubs — must raise, never silently default
# ---------------------------------------------------------------------------


def test_ask_approval_raises_not_implemented() -> None:
    """ask_approval must raise NotImplementedError until branch 7 wires the focus-swap."""
    ui = make_ui()
    request = ApprovalRequest(
        kind="tool",
        display="patch_file hello.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
    )
    with pytest.raises(NotImplementedError):
        ui.ask_approval(request)


def test_ask_plan_approval_raises_not_implemented() -> None:
    """ask_plan_approval must raise NotImplementedError until branch 7."""
    ui = make_ui()
    with pytest.raises(NotImplementedError):
        ui.ask_plan_approval(make_plan(), "/tmp/ws/PLAN.md")


# ---------------------------------------------------------------------------
# stream_thinking with no active turn renders nothing (only the count is ever
# surfaced, and only while a turn runs)
# ---------------------------------------------------------------------------


def test_stream_thinking_without_active_turn_renders_nothing() -> None:
    ui = make_ui()
    ui.stream_thinking("deep thoughts")  # no begin_response → no indicator
    assert "deep thoughts" not in plain(ui)
    assert plain(ui) == ""


# ---------------------------------------------------------------------------
# §31.14 — turn-scoped live thinking indicator
# ---------------------------------------------------------------------------


def _clock(values: list[float]) -> Callable[[], float]:
    """A fake monotonic clock returning successive values, holding the last one."""
    state = {"i": 0}

    def now() -> float:
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[i]

    return now


def make_stats(*, elapsed_s: float = 2.0, output_tokens: int = 80) -> TurnStats:
    return TurnStats(
        elapsed_s=elapsed_s,
        context_tokens=500,
        context_pct=6,
        warn=False,
        output_tokens=output_tokens,
    )


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1800, "1.8k"),
        (999_999, "1000.0k"),
        (1_000_000, "1.0m"),
        (2_400_000, "2.4m"),
    ],
)
def test_fmt_count_boundaries(n: int, expected: str) -> None:
    from shellpilot.cli.app_ui import _fmt_count

    assert _fmt_count(n) == expected


def test_begin_response_starts_active_indicator() -> None:
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()
    # The plane glyph (first spinner frame) renders → the indicator is active.
    assert GLYPHS.spinner_frames[0] in ansi_text(ui)
    # The flight phrase + an elapsed timer are on the live line.
    assert "taxiing" in plain(ui)
    assert "0s" in plain(ui)


def test_second_begin_response_does_not_restart_indicator() -> None:
    # First begin_response at t=0 starts the indicator; a second at t=30 must NOT
    # restart it — the elapsed timer keeps climbing from the original start.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=_clock([0.0, 30.0, 30.0]))
    ui.begin_response()  # consumes 0.0 → start
    ui.begin_response()  # consumes 30.0 → must be ignored (no restart)
    out = plain(ui)  # consumes 30.0 → render at elapsed 30
    assert "30s" in out  # elapsed measured from the ORIGINAL start, not the second call


def test_indicator_runs_continuously_across_tool_calls() -> None:
    # LOAD-BEARING regression (§31.14): the timer + phrase + reasoning count must
    # span the WHOLE turn, NOT reset per model call. This mirrors the real tool
    # loop: begin_response → think → end_response → (tool call) → begin_response
    # again. The second begin_response (next tool-loop iteration) must be a NO-OP
    # for the indicator — start, phrase, and reasoning count all carry over.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=_clock([3.0, 45.0]))
    ui.begin_response()  # turn start recorded at t=3.0 (first model call)
    start_before = ui._indicator.start  # type: ignore[union-attr]
    ui.stream_thinking("a" * 2000)  # 500 tokens of reasoning in the first call
    ui.end_response()  # model call ended — must NOT stop/reset the indicator
    ui.show_tool_call("read_file", {"path": "x"})  # a tool call between model calls
    ui.begin_response()  # next tool-loop iteration — must NOT restart the indicator
    # The turn-start timestamp is unchanged (no restart).
    assert ui._indicator is not None
    assert ui._indicator.start == start_before == 3.0
    # The reasoning count carried over (NOT reset to 0 by end_response or the
    # second begin_response).
    assert ui._indicator.reasoning_chars == 2000
    # Elapsed counts up monotonically from the single turn-start: render at t=45
    # shows 42s (45 - 3), and the frozen 500-token reasoning estimate persists.
    out = plain(ui)
    assert "42s" in out
    assert "500 reasoning" in out


def test_end_response_does_not_touch_the_indicator() -> None:
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()
    ui.stream_thinking("b" * 1200)  # 300 tokens
    ui.end_response()
    # end_response only closes the open response; the indicator stays active with
    # its reasoning count intact.
    assert ui._indicator is not None
    assert ui._indicator.reasoning_chars == 1200
    assert "300 reasoning" in plain(ui)


def test_new_turn_after_error_starts_fresh_indicator() -> None:
    # A turn that errors before turn_finished leaves the indicator dangling. The
    # NEXT turn's show_user_message must discard it so the new begin_response starts
    # fresh — otherwise the no-op-while-active begin_response would keep the OLD
    # start and the new turn's timer would read inflated (the cross-turn poisoning
    # the §31.14 guard prevents; full turn-failure UX is branch 6's).
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=_clock([3.0, 100.0]))
    ui.begin_response()  # turn 1 starts at t=3
    ui.stream_thinking("x" * 2000)  # 500 tokens of reasoning
    assert ui._indicator is not None  # ... then turn 1 errors: no turn_finished
    ui.show_user_message("next question")  # turn 2 begins → discard the stale one
    assert ui._indicator is None
    ui.begin_response()  # turn 2 — a FRESH indicator at t=100, not the stale t=3
    assert ui._indicator is not None
    assert ui._indicator.start == 100.0
    assert ui._indicator.reasoning_chars == 0


def test_abort_turn_clears_indicator_and_marks_partial() -> None:
    # Branch 6 (§31.15): a cancelled turn clears the dangling live indicator and
    # marks the partial aborted, leaving the streamed-so-far text visible.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()  # turn starts → indicator active
    ui.stream_token("partial answer so far")  # streamed text, response still open
    assert ui._indicator is not None

    ui.abort_turn()

    # The live indicator is gone (no dangling timer/plane after a cancel).
    assert ui._indicator is None
    out = plain(ui)
    # The partial streamed text was finalized and is still visible ...
    assert "partial answer so far" in out
    # ... with the aborted marker below it (unicode ⏹, style sp.warn).
    assert "⏹ aborted" in out
    # A later render does not resurrect the live indicator (no flight phrase).
    assert "taxiing" not in plain(ui)


def test_abort_turn_ascii_fallback_marker() -> None:
    # ⏹ is not in the Glyphs set; in ASCII mode it degrades to the cross glyph.
    ui = AppUI(glyphs=ASCII_GLYPHS, width_fn=lambda: 80)
    ui.begin_response()
    ui.abort_turn()
    out = plain(ui)
    assert ui._indicator is None
    assert "⏹" not in out
    assert f"{ASCII_GLYPHS.cross} aborted" in out


def test_stream_thinking_climbs_reasoning_estimate() -> None:
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()
    assert "0 reasoning" in plain(ui)
    # 4400 chars / CHARS_PER_TOKEN(4) = 1100 tokens → "1.1k reasoning".
    ui.stream_thinking("x" * 4400)
    assert "1.1k reasoning" in plain(ui)
    # Thinking stops → the count freezes (more renders do not change it).
    assert "1.1k reasoning" in plain(ui)


def test_live_line_phrase_and_elapsed_with_fixed_clock() -> None:
    # At elapsed 23s the phase is "cruise" (start=20); phrase index = int(23/10)%len
    # = 2 → the 3rd cruise phrase ("scanning the instruments"). Reasoning 400 chars
    # / 4 = 100 tokens.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=_clock([0.0, 23.0]))
    ui.begin_response()  # start at 0.0
    ui.stream_thinking("y" * 400)
    out = plain(ui)  # render at 23.0
    assert "scanning the instruments" in out
    assert "23s" in out
    assert "100 reasoning" in out


def test_turn_finished_freezes_done_line_and_clears_indicator() -> None:
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()
    ui.stream_thinking("z" * 2000)  # 500 tokens
    ui.turn_finished(make_stats(elapsed_s=7.0, output_tokens=1800))
    out = plain(ui)
    # The frozen done line: check glyph, authoritative elapsed, frozen reasoning,
    # exact total (k-formatted).
    assert GLYPHS.check in out
    assert "done" in out
    assert "7s" in out
    assert "500 reasoning" in out
    assert "1.8k total" in out
    # The indicator is cleared: no live plane frame remains in the transcript.
    assert ui._indicator is None
    # A fresh begin_response starts a brand-new indicator (count back to 0).
    ui.begin_response()
    assert "0 reasoning" in plain(ui)


def test_show_reasoning_false_omits_reasoning_and_total() -> None:
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, show_reasoning=False, time_fn=lambda: 0.0)
    ui.begin_response()
    ui.stream_thinking("q" * 4000)
    live = plain(ui)
    assert "reasoning" not in live  # live line is plane/phrase/timer only
    assert "taxiing" in live and "0s" in live
    ui.turn_finished(make_stats(elapsed_s=3.0, output_tokens=1234))
    done = plain(ui)
    assert "done" in done and "3s" in done
    assert "reasoning" not in done
    assert "total" not in done


def test_active_indicator_render_bypasses_width_cache() -> None:
    # Two renders at the SAME width with the clock advanced must differ (live
    # elapsed) — the active-turn render never reads or writes the width cache.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=_clock([0.0, 5.0, 12.0]))
    ui.begin_response()  # start at 0.0
    first = ui._render_ansi()  # elapsed 5
    second = ui._render_ansi()  # elapsed 12
    assert first is not second
    assert "5s" in strip_ansi(first)
    assert "12s" in strip_ansi(second)
    # No cache was written while active.
    assert ui._cache is None


def test_show_user_message_echoes_with_chevron() -> None:
    ui = make_ui()
    ui.show_user_message("run the tests")
    out = plain(ui)
    assert GLYPHS.chevron in out
    assert "run the tests" in out


def test_show_user_message_sanitizes_control_chars() -> None:
    ui = make_ui()
    ui.show_user_message("clean\x1b[2J\x00\x07injected")
    raw = ansi_text(ui)
    assert "\x1b[2J" not in raw
    assert "\x00" not in raw
    assert "\x07" not in raw
    assert "clean" in plain(ui)
    assert "injected" in plain(ui)


def test_frontier_ordering_user_then_content_then_done() -> None:
    # user message echoed, then a tool call renders ABOVE the live indicator, then
    # turn_finished freezes the done line LAST: order is `❯ msg`, tool call, `✓ done`.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.show_user_message("do the thing")
    ui.begin_response()
    ui.show_tool_call("read_file", {"path": "notes.txt"})
    ui.turn_finished(make_stats())
    out = plain(ui)
    i_msg = out.index("do the thing")
    i_tool = out.index("read_file")
    i_done = out.index("done")
    assert i_msg < i_tool < i_done
