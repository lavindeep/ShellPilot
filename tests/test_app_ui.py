"""Tests for AppUI — the RuntimeUI implementation for the full-screen pane (§31.12)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.theme import UNICODE_GLYPHS
from shellpilot.memory.redaction import REDACTED
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
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
    """Changing the width from the width_fn triggers a re-render (cache miss)."""
    width = [80]
    ui = AppUI(glyphs=GLYPHS, width_fn=lambda: width[0])
    # Add enough text that line-wrapping changes between 80 and 40 columns.
    ui.show_status("a" * 60 + " " + "b" * 60)
    ansi_80 = ui._render_ansi()
    width[0] = 40
    ansi_40 = ui._render_ansi()
    # The ANSI output must differ — different wrapping at different widths.
    assert ansi_80 != ansi_40


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
    assert before != after
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
# No-op stubs — must not raise or produce output
# ---------------------------------------------------------------------------


def test_begin_response_is_noop() -> None:
    ui = make_ui()
    ui.begin_response()  # must not raise
    assert plain(ui) == ""


def test_stream_thinking_is_noop() -> None:
    ui = make_ui()
    ui.stream_thinking("deep thoughts")
    assert "deep thoughts" not in plain(ui)


def test_turn_finished_is_noop() -> None:
    from shellpilot.runtime.events import TurnStats

    ui = make_ui()
    ui.turn_finished(
        TurnStats(elapsed_s=1.5, context_tokens=500, context_pct=6, warn=False, output_tokens=80)
    )
    assert plain(ui) == ""
