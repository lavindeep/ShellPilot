"""Snapshot-style tests for the themed renderer components (design section 31)."""

from __future__ import annotations

import difflib
import io
from pathlib import Path

from rich.console import Console, RenderableType

from shellpilot.cli.render import (
    approval_block,
    badge,
    banner,
    context_line,
    output_truncation,
    plan_panel,
    plan_step_line,
    render_diff,
    tool_call,
    tool_result,
    turn_stats,
    word_highlight_ranges,
)
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan

GLYPHS = UNICODE_GLYPHS


def rendered(renderable: RenderableType, width: int = 100) -> str:
    console = Console(record=True, width=width, file=io.StringIO(), theme=SHELLPILOT_THEME)
    console.print(renderable)
    return console.export_text()


def make_diff(before: str, after: str, name: str = "hello.py") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def test_banner_shows_version_and_model() -> None:
    out = rendered(banner("9.9.9", "gemma4:e4b", "balanced"))
    assert "ShellPilot 9.9.9" in out
    assert "gemma4:e4b · balanced" in out


def test_context_line_abbreviates_home() -> None:
    home = Path("/Users/someone")
    out = rendered(context_line(home / "proj", "gemma4:e4b", "balanced", home=home))
    assert "~/proj · gemma4:e4b · balanced" in out
    assert "/Users/someone" not in out


def test_context_line_truncates_long_paths() -> None:
    home = Path("/Users/someone")
    deep = home / ("very/deep/" + "x" * 120 + "/end")
    text = context_line(deep, "gemma4:e4b", "balanced", home=home, max_width=60)
    assert text.cell_len <= 60
    assert "…" in text.plain
    assert text.plain.endswith("· gemma4:e4b · balanced")


def test_tool_call_bolds_name_and_dims_args() -> None:
    line = tool_call("patch_file", "hello.py · 1 addition", GLYPHS)
    assert f"{GLYPHS.bullet} patch_file" in line.plain
    assert "(hello.py · 1 addition)" in line.plain
    assert any(span.style == "sp.emph" for span in line.spans)


def test_tool_result_marks_success_and_failure() -> None:
    ok = tool_result(True, "exit 0 · 0.4s", GLYPHS)
    bad = tool_result(False, "exit 2", GLYPHS)
    assert GLYPHS.check in ok.plain and "exit 0" in ok.plain
    assert GLYPHS.cross in bad.plain


def test_tool_renderers_sanitize_external_text() -> None:
    call = tool_call("patch_file\x1b[2J\x00", "path=\tfile.py\x07", GLYPHS)
    result = tool_result(True, "updated\x0b file.py\x7f", GLYPHS)

    for output in (call.plain, result.plain):
        assert not any(char in output for char in "\x1b\x00\x07\x0b\x7f\t")
    assert "patch_file" in call.plain
    assert "path=" in call.plain and "file.py" in call.plain
    assert "updated file.py" in result.plain


def test_word_highlight_ranges_similar_pair() -> None:
    result = word_highlight_ranges('print("done")', 'print("Goodbye World")')
    assert result is not None
    old_ranges, new_ranges = result
    assert old_ranges and new_ranges
    start, end = new_ranges[0]
    assert "Goodbye" in 'print("Goodbye World")'[start:end]


def test_word_highlight_ranges_dissimilar_pair_is_none() -> None:
    assert word_highlight_ranges("import os", 'print("totally different")') is None


def test_render_diff_shows_panel_with_line_numbers() -> None:
    diff = make_diff(
        'print("Hello World")\nprint("done")\n',
        'print("Hello World")\nprint("Goodbye World")\n',
    )
    out = rendered(render_diff(diff, GLYPHS))
    assert "hello.py" in out
    assert '- print("done")' in out
    assert '+ print("Goodbye World")' in out
    assert "1" in out and "2" in out
    assert "╭" in out and "╰" in out  # rich-drawn rounded panel


def test_render_diff_pure_add_and_remove() -> None:
    add_only = make_diff("a\n", "a\nb\n")
    remove_only = make_diff("a\nb\n", "a\n")
    assert "+ b" in rendered(render_diff(add_only, GLYPHS))
    assert "- b" in rendered(render_diff(remove_only, GLYPHS))


def test_render_diff_sanitizes_tabs_crlf_and_truncation_marker() -> None:
    diff = make_diff("x\r\n", "x\r\n\tindented\r\n") + "... (42 more lines)\n"
    out = rendered(render_diff(diff, GLYPHS))
    assert "\r" not in out
    assert "\t" not in out
    assert "indented" in out
    assert "42 more lines" in out


def test_badge_chips_and_plain_degradation() -> None:
    assert badge("medium").plain == " MEDIUM "
    assert badge("high").plain == " HIGH "
    assert badge("blocked", plain=True).plain == "[BLOCKED]"


def test_approval_block_composes_display_reasons_and_cwd() -> None:
    request = ApprovalRequest(
        kind="command",
        display="rm -rf build/",
        risk=RiskLevel.HIGH,
        reasons=("recursive delete",),
        cwd=Path("/tmp/ws"),
        purpose="Removes stale build output.",
    )
    out = rendered(approval_block(request, GLYPHS))
    assert "rm -rf build/" in out
    assert " HIGH " in out
    assert "recursive delete" in out
    assert "Removes stale build output." in out
    assert "/tmp/ws" in out


def test_plan_panel_gate_and_step_lines() -> None:
    plan = TaskPlan(
        task_id="20260611-031500-demo-task",
        goal="Do the demo",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[
            PlanStep(title="First", status="completed"),
            PlanStep(title="Second", status="active"),
            PlanStep(title="Third"),
        ],
    )
    out = rendered(plan_panel(plan, GLYPHS))
    assert "Plan · 20260611-031500-demo-task" in out
    assert "Goal: Do the demo" in out
    assert f"{GLYPHS.check} 1" in out
    assert f"{GLYPHS.current} 2" in out
    assert f"{GLYPHS.todo} 3" in out

    line = plan_step_line(2, PlanStep(title="Second", status="active"), GLYPHS)
    assert f"{GLYPHS.current} 2" in line.plain and "Second" in line.plain


def test_turn_stats_formats_and_warns() -> None:
    calm = turn_stats(2.13, 1_400, 18, warn=False)
    assert "2.1s · 1.4k tokens · ctx 18%" in calm.plain
    assert not any(span.style == "sp.warn" for span in calm.spans)

    hot = turn_stats(0.5, 812, 84, warn=True)
    assert "812 tokens" in hot.plain
    assert any(span.style == "sp.warn" for span in hot.spans)


def test_output_truncation_marker() -> None:
    assert "+214 lines" in output_truncation(214, GLYPHS).plain
