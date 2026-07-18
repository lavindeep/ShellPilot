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


def test_intro_renders_as_first_pane_content() -> None:
    """The boot banner passed as `intro` is seeded into the pane (§31.13).

    Without this it console.prints behind the alt-screen and is never seen.
    """
    from rich.text import Text

    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, intro=Text("BANNER-MARKER"))
    assert "BANNER-MARKER" in ui._render_ansi()


def test_clear_conversation_preserves_intro_banner() -> None:
    from rich.text import Text

    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, intro=Text("BANNER-MARKER"))
    ui.show_user_message("old visible transcript")

    ui.clear_conversation("Conversation cleared.")

    out = plain(ui)
    assert "BANNER-MARKER" in out
    assert "old visible transcript" not in out
    assert "Conversation cleared." in out


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


def test_responses_render_via_response_markdown() -> None:
    """Both the committed and the in-progress response use the shared builder
    (§31.7): sanitized markdown with the ANSI code theme, so fenced code never
    paints monokai's background into the pane."""
    from rich.markdown import Markdown

    ui = make_ui()
    ui.stream_token("```python\nx = 1\n```")
    ui.end_response()
    committed = ui._renderables[-1]
    assert isinstance(committed, Markdown)
    assert committed.code_theme == "ansi_dark"


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
# Approval prompt content (§31.16) — the pane block for the focus-swap gate
# ---------------------------------------------------------------------------


def test_show_approval_renders_diff_info_and_cwd() -> None:
    ui = make_ui(workspace=Path("/tmp/ws"))
    request = ApprovalRequest(
        kind="command",
        display="patch x.py",
        risk=RiskLevel.HIGH,
        reasons=("recursive delete",),
        cwd=Path("/tmp/ws"),
        diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-oldline\n+newline\n",
    )
    ui.show_approval(request)
    text = plain(ui)
    assert "HIGH" in text  # risk badge
    assert "recursive delete" in text  # classifier reason (the WHY row)
    assert "CWD" in text and "/tmp/ws" in text  # working directory (stat block)
    assert "oldline" in text and "newline" in text  # the diff content rendered


def test_show_approval_renders_the_risk_card() -> None:
    """The badge + stat rows + CWD arrive as ONE risk-bordered panel (§31.5),
    not loose flush-left lines."""
    from rich.panel import Panel

    ui = make_ui()
    request = ApprovalRequest(
        kind="tool",
        display="patch_file hello.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
    )
    ui.show_approval(request)
    cards = [r for r in ui._renderables if isinstance(r, Panel)]
    assert len(cards) == 1
    assert str(cards[0].border_style) == "sp.warn"


def test_show_approval_without_diff_omits_panel() -> None:
    ui = make_ui()
    request = ApprovalRequest(
        kind="tool",
        display="patch_file hello.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
    )
    ui.show_approval(request)
    text = plain(ui)
    assert "MEDIUM" in text
    assert "writes inside workspace" in text


def test_show_plan_approval_renders_panel_and_path() -> None:
    ui = make_ui()
    ui.show_plan_approval(make_plan(), "/tmp/ws/PLAN.md")
    text = plain(ui)
    assert "Demo goal" in text  # plan panel goal
    assert "/tmp/ws/PLAN.md" in text  # the artifact path


def test_show_plan_approval_sanitizes_path() -> None:
    ui = make_ui()
    ui.show_plan_approval(make_plan(), "/tmp/ws/PL\x07AN.md")  # embedded bell
    text = plain(ui)
    assert "\x07" not in text
    assert "/tmp/ws/PLAN.md" in text


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


def test_is_animating_gates_to_active_turn() -> None:
    # is_animating drives run_app's gated refresh loop: True only while a turn is in
    # flight, so an idle app schedules no timer redraws (§31.14, perf).
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    assert ui.is_animating is False  # idle
    ui.begin_response()
    assert ui.is_animating is True  # turn in flight → animate
    ui.turn_finished(make_stats())
    assert ui.is_animating is False  # frozen done line, indicator cleared → idle again


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


def test_fail_turn_clears_indicator_and_shows_error_without_marker() -> None:
    # A turn that RAISED (e.g. a network/API error) must tear down the dangling
    # live indicator and surface the error — but NOT the "aborted" marker, which
    # is reserved for a clean user Ctrl-C (abort_turn). This is the gap that left
    # the thinking indicator on screen after a cloud 502.
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)
    ui.begin_response()  # turn starts → indicator active
    ui.stream_token("partial answer so far")
    assert ui._indicator is not None

    ui.fail_turn("The cloud model is unavailable or timed out (HTTP 502). Retry.")

    assert ui._indicator is None  # no dangling timer/plane after a failure
    out = plain(ui)
    assert "partial answer so far" in out  # partial finalized, still visible
    assert "HTTP 502" in out  # the friendly error line is shown
    assert "aborted" not in out  # NOT the user-cancel marker
    assert "taxiing" not in plain(ui)  # a later render does not resurrect it


def test_show_idle_hint_dedups_until_next_turn() -> None:
    # Repeated Ctrl-C while idle must not stack the hint (the screenshot showed it
    # 4×). One hint per idle period; a new turn (show_user_message) re-arms it.
    ui = make_ui()
    for _ in range(3):
        ui.show_idle_hint("(idle — type /exit to quit)")
    assert plain(ui).count("(idle") == 1

    ui.show_user_message("next turn")
    ui.show_idle_hint("(idle — type /exit to quit)")
    assert plain(ui).count("(idle") == 2


def test_clear_conversation_rearms_idle_hint() -> None:
    # After /clear wipes the pane, a fresh idle Ctrl-C should show the hint again.
    ui = make_ui()
    ui.show_idle_hint("(idle — type /exit to quit)")
    ui.clear_conversation("Conversation cleared.")
    ui.show_idle_hint("(idle — type /exit to quit)")
    assert plain(ui).count("(idle") == 1  # the pre-clear hint was wiped; one fresh hint


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


def test_show_user_message_brightens_text_and_accents_chevron() -> None:
    """The echo carries brightness hierarchy (§31.12): green chevron, bright
    message — not the old all-accent line that colored the user's own words."""
    from rich.text import Text

    ui = make_ui()
    ui.show_user_message("run the tests")
    echo = ui._renderables[-1]
    assert isinstance(echo, Text)
    styles = {str(span.style) for span in echo.spans}
    assert "sp.chevron" in styles
    assert "sp.emph" in styles
    assert "sp.accent" not in styles


def test_show_user_message_adds_breathing_room_between_turns() -> None:
    """A blank spacer line precedes the echo when the transcript already has
    content, so consecutive turns don't blur together — but the first message
    into an empty pane gets no leading blank."""
    from rich.text import Text

    ui = make_ui()
    ui.show_user_message("first turn")
    first = ui._renderables[0]
    assert isinstance(first, Text)
    assert first.plain != ""  # no leading blank on an empty pane

    ui.turn_finished(make_stats(elapsed_s=1.0, output_tokens=5))
    ui.show_user_message("second turn")
    echo_index = next(
        i for i, r in enumerate(ui._renderables) if isinstance(r, Text) and "second turn" in r.plain
    )
    spacer = ui._renderables[echo_index - 1]
    assert isinstance(spacer, Text)
    assert spacer.plain == ""  # blank line between the done line and the echo


def test_show_user_message_sanitizes_control_chars() -> None:
    ui = make_ui()
    ui.show_user_message("clean\x1b[2J\x00\x07injected")
    raw = ansi_text(ui)
    assert "\x1b[2J" not in raw
    assert "\x00" not in raw
    assert "\x07" not in raw
    assert "clean" in plain(ui)
    assert "injected" in plain(ui)


def test_clear_conversation_resets_visible_transcript_state() -> None:
    ui = make_ui()
    ui.show_user_message("before clear")
    ui.begin_response()
    ui.stream_thinking("hidden chain")
    ui.stream_token("partial answer")

    ui.clear_conversation("Conversation cleared.")

    out = plain(ui)
    assert "before clear" not in out
    assert "hidden chain" not in out
    assert "partial answer" not in out
    assert "Conversation cleared." in out
    assert ui.is_animating is False
    assert ui._open_response is None
    assert ui._active_trail is None
    assert ui._toggle_ranges == []


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


# ---------------------------------------------------------------------------
# §31.19 — inline collapsible thinking trail
# ---------------------------------------------------------------------------


def _trail_ui() -> AppUI:
    return AppUI(glyphs=GLYPHS, width_fn=lambda: 80, time_fn=lambda: 0.0)


def _click_toggle(ui: AppUI, element: object) -> bool:
    # The click model (§31.16/§31.19): render to populate the transcript line
    # index, then toggle *element* via a line inside its range — exactly what a
    # pane click maps to. Returns toggle_at's result (False if not on screen).
    ui._render_ansi()
    for start, _end, el in ui._toggle_ranges:
        if el is element:
            return ui.toggle_at(start)
    return False


def test_thinking_trail_created_collapsed_no_footer() -> None:
    # stream_thinking during an active turn builds an inline trail, collapsed by
    # default; with <=10 lines all are shown and there is NO hidden-lines footer.
    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("alpha\nbeta\ngamma")
    out = plain(ui)
    assert "thinking" in out  # the trail header
    assert "alpha" in out and "beta" in out and "gamma" in out
    assert "hidden lines" not in out


def test_thinking_trail_collapsed_hides_overflow_with_footer() -> None:
    # More than TRAIL_COLLAPSED_LINES non-blank lines → first 6 shown, the rest
    # hidden behind the exact footer wording with the correct remainder count.
    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("\n".join(f"line{i}" for i in range(15)))
    out = plain(ui)
    for i in range(6):
        assert f"line{i}" in out  # first 6 shown
    assert "line6" not in out  # 7th+ hidden
    assert "+9 hidden lines · click to expand" in out


def test_click_expands_and_collapses_trail() -> None:
    # Clicking a trail (toggle_at on a line in its range) expands it to all lines +
    # the collapse hint; clicking again collapses back to the first 6 + the footer.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("\n".join(f"row{i}" for i in range(15)))
    trail = next(r for r in ui._renderables if isinstance(r, _Trail))
    assert _click_toggle(ui, trail) is True
    out = plain(ui)
    for i in range(15):
        assert f"row{i}" in out
    assert "click to collapse" in out
    assert "hidden lines" not in out
    assert _click_toggle(ui, trail) is True
    out2 = plain(ui)
    assert "row14" not in out2
    assert "+9 hidden lines · click to expand" in out2


def test_click_outside_any_element_returns_false() -> None:
    ui = make_ui()
    ui._render_ansi()  # populate the (empty) line index
    assert ui.toggle_at(0) is False  # nothing toggleable here → no-op, no raise


def test_show_reasoning_false_builds_no_trail() -> None:
    # With the reasoning readout off, no trail is built and there is nothing to
    # toggle; the live indicator stays free of any reasoning readout (existing).
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: 80, show_reasoning=False, time_fn=lambda: 0.0)
    ui.begin_response()
    ui.stream_thinking("secret thoughts here")
    out = plain(ui)  # also populates the line index
    assert "thinking" not in out
    assert "secret thoughts here" not in out
    assert ui._toggle_ranges == []  # nothing toggleable
    assert ui.toggle_at(0) is False
    assert "reasoning" not in out


def test_two_phases_each_trail_clickable_independently() -> None:
    # Two reasoning phases separated by a tool call produce two distinct trail
    # blocks; clicking one toggles ONLY it — older trails stay reachable (the win
    # over a single latest-only toggle).
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("phase one A\nphase one B")
    ui.show_tool_call("read_file", {"path": "x"})
    ui.stream_thinking("phase two A\nphase two B")
    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    assert len(trails) == 2
    assert _click_toggle(ui, trails[0]) is True  # click the OLDER trail
    assert trails[0].expanded is True
    assert trails[1].expanded is False  # the other one is untouched
    assert _click_toggle(ui, trails[1]) is True  # click the newer trail
    assert trails[1].expanded is True


def test_new_turn_trail_defaults_collapsed_older_keeps_state() -> None:
    # A fresh turn's trail is collapsed even if the prior turn's trail was expanded;
    # the older trail keeps its expanded state.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("first turn thinking")
    first = next(r for r in ui._renderables if isinstance(r, _Trail))
    assert _click_toggle(ui, first) is True
    assert first.expanded is True
    ui.show_user_message("next")
    ui.begin_response()
    ui.stream_thinking("second turn thinking")
    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    second = trails[1]
    assert second is not first
    assert second.expanded is False
    assert first.expanded is True


def test_abort_turn_preserves_trail_state() -> None:
    # abort_turn keeps the active trail visible and never resets a prior finished
    # trail's expanded state; the active-trail pointer is cleared afterwards.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("phase one thoughts")
    ui.show_tool_call("read_file", {"path": "x"})  # finalize phase one
    first = next(r for r in ui._renderables if isinstance(r, _Trail))
    assert _click_toggle(ui, first) is True  # expand the finished trail
    ui.stream_thinking("phase two thoughts")  # a fresh active trail
    ui.abort_turn()
    out = plain(ui)
    assert "phase two thoughts" in out  # active trail still visible
    assert ui._active_trail is None
    assert first.expanded is True


def test_show_user_message_finalizes_active_trail() -> None:
    # A new turn's user echo finalizes a dangling active trail without resetting
    # any prior trail's expanded state.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("dangling thoughts")
    assert ui._active_trail is not None
    trail = next(r for r in ui._renderables if isinstance(r, _Trail))
    assert _click_toggle(ui, trail) is True
    ui.show_user_message("new question")
    assert ui._active_trail is None
    assert trail.expanded is True


def test_interleaved_thinking_after_answer_keeps_one_trail_and_response() -> None:
    # Regression (§31.19): a model that emits a trailing reasoning fragment AFTER
    # the answer has started must NOT fragment the turn into two trails and two
    # response blocks. Within one model call the thinking is one trail and the
    # answer is one response, regardless of interleaving order.
    from rich.markdown import Markdown

    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("the user said hi; keep it short, concise")
    ui.stream_token("Hello!")
    ui.stream_thinking(" answers.")  # the reasoning's tail, after the answer began
    ui.stream_token(" How can I help you today?")
    ui.end_response()

    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    responses = [r for r in ui._renderables if isinstance(r, Markdown)]
    assert len(trails) == 1  # one trail, not two
    assert len(responses) == 1  # one response, not split
    assert "keep it short, concise" in trails[0].text
    assert "answers." in trails[0].text  # the trailing fragment joined the SAME trail
    assert "Hello! How can I help you today?" in responses[0].markup


def test_answer_first_then_thinking_keeps_one_response() -> None:
    # The mirror case: the answer streams BEFORE any reasoning arrives, then a
    # thought lands mid-answer. The thought must NOT close the open response —
    # the answer stays one block (guards stream_thinking not calling
    # _close_open_response when it opens a trail).
    from rich.markdown import Markdown

    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_token("Hello!")
    ui.stream_thinking("a passing thought")  # thinking after the answer began
    ui.stream_token(" How can I help you today?")
    ui.end_response()

    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    responses = [r for r in ui._renderables if isinstance(r, Markdown)]
    assert len(responses) == 1  # one response, not split by the mid-answer thought
    assert len(trails) == 1
    assert "Hello! How can I help you today?" in responses[0].markup


def test_separate_model_calls_still_make_separate_trails() -> None:
    # The flip side of the interleave fix: a genuine second reasoning phase in a
    # LATER model call (the tool-loop boundary, end_response → begin_response)
    # still opens its OWN trail — end_response finalizes the call's trail.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("call one thinking")
    ui.stream_token("answer one")
    ui.end_response()
    ui.begin_response()
    ui.stream_thinking("call two thinking")
    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    assert len(trails) == 2
    assert "call one thinking" in trails[0].text
    assert "call two thinking" in trails[1].text


def test_trail_sanitizes_control_chars() -> None:
    # Thinking text is model-controlled → every displayed line is sanitized; no raw
    # BEL or escape-injection bytes reach the pane.
    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("danger\x07\x1b[31mred")
    raw = ansi_text(ui)
    assert "\x07" not in raw
    assert "\x1b[31m" not in raw
    assert "danger" in plain(ui)


def test_trail_header_reasoning_count() -> None:
    # The trail header carries the reasoning-token estimate (chars / CHARS_PER_TOKEN),
    # matching the indicator's estimate.
    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("x" * 2000)  # 2000 / 4 = 500 tokens
    assert "500 reasoning" in plain(ui)


def test_show_plan_progress_finalizes_active_trail() -> None:
    # show_plan_progress is the one content-appender that bypasses _add_renderable;
    # it must still finalize the active trail so the §31.19 invariant holds and the
    # next reasoning phase opens a fresh block (not merge into the prior one).
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("pre-plan thoughts")
    assert ui._active_trail is not None
    ui.show_plan_progress(make_plan())
    assert ui._active_trail is None  # finalized
    ui.stream_thinking("post-plan thoughts")
    trails = [r for r in ui._renderables if isinstance(r, _Trail)]
    assert len(trails) == 2  # a fresh trail, not appended to the first


def test_click_finished_trail_while_idle_rerenders() -> None:
    # Clicking a FINISHED trail after the turn ended (indicator None → the width
    # cache is live) must still re-render: the toggle invalidates the cache, so the
    # newly-shown lines appear. Guards the idle-toggle path the live UI uses.
    from shellpilot.cli.app_ui import _Trail

    ui = _trail_ui()
    ui.begin_response()
    ui.stream_thinking("\n".join(f"idle{i}" for i in range(15)))
    ui.turn_finished(make_stats())  # indicator → None
    assert ui._indicator is None
    assert "idle14" not in plain(ui)  # collapsed: 15th line hidden
    trail = next(r for r in ui._renderables if isinstance(r, _Trail))
    assert _click_toggle(ui, trail) is True
    assert "idle14" in plain(ui)  # expanded render reflects the toggle (cache busted)


# ---------------------------------------------------------------------------
# Live workspace (workspace_fn): mid-session /cwd staleness fix
# ---------------------------------------------------------------------------


def test_tool_call_path_uses_live_workspace(tmp_path: Path) -> None:
    """workspace_fn is re-evaluated at each show_tool_call, so a mid-session
    /cwd change is honoured immediately in the tool-call summary line."""
    from shellpilot.tools.base import OUTSIDE_WORKSPACE_DISPLAY

    ws_a = tmp_path
    ws_b = tmp_path / "sub"
    holder: dict[str, Path] = {"ws": ws_a}

    # workspace_fn returns the live workspace from the holder and must win over
    # the deliberately stale static fallback.
    ui = AppUI(
        glyphs=GLYPHS,
        workspace=ws_b,
        workspace_fn=lambda: holder["ws"],
        width_fn=lambda: 80,
    )

    # The absolute path is inside ws_a but outside ws_b.
    abs_path = str(ws_a / "file.txt")
    ui.show_tool_call("read_file", {"path": abs_path})
    out_before = plain(ui)
    # Under ws_a: resolves to the relative "file.txt" — inside workspace.
    assert "file.txt" in out_before
    assert OUTSIDE_WORKSPACE_DISPLAY not in out_before

    # Simulate /cwd set to ws_b — the same abs_path is now outside the workspace.
    holder["ws"] = ws_b
    ui.show_tool_call("read_file", {"path": abs_path})
    out_after = plain(ui)
    # The SECOND tool-call line must reflect the live workspace, not the stale one.
    assert OUTSIDE_WORKSPACE_DISPLAY in out_after
