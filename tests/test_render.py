"""Snapshot-style tests for the themed renderer components (design section 31)."""

from __future__ import annotations

import difflib
import io
from pathlib import Path

from rich.console import Console, RenderableType

from shellpilot.cli.render import (
    approval_block,
    badge,
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


def test_render_diff_changed_lines_are_separate_full_width_bars() -> None:
    """A changed line renders as its own red removal row then its own green
    addition row (DESIGN section 31.4 full-line backgrounds) — never paired onto
    one visual line. Each changed row's colored background must span the full
    content width so removal (red) and addition (green) read as distinct bars.
    """
    from shellpilot.cli.render import _diff_rows

    # Removal shorter than its addition: without full-width fill the red bar
    # would stop mid-line and the two rows would not read as separate diff lines.
    diff = make_diff('print("done")\n', 'print("Goodbye World")\n')
    rows, _ = _diff_rows(diff, GLYPHS)
    remove_row = next(r for r in rows if "- " in r.plain)
    add_row = next(r for r in rows if "+ " in r.plain)

    def base_bg_end(row: object, style: str) -> int:
        return next(span.end for span in row.spans if span.style == style)  # type: ignore[attr-defined]

    remove_end = base_bg_end(remove_row, "sp.diff.remove")
    add_end = base_bg_end(add_row, "sp.diff.add")
    # Both colored backgrounds reach the same full content width: the removal's
    # red bar is padded to match the longer addition's green bar.
    assert remove_end == add_end
    # And the fill genuinely extends past the removal's own short text (the bug:
    # the red bar stopped at len("- print(\"done\")") instead of filling).
    assert remove_end > len('- print("done")') + 2  # gutter ("1 ") + text


def test_render_diff_sanitizes_tabs_crlf_and_truncation_marker() -> None:
    diff = make_diff("x\r\n", "x\r\n\tindented\r\n") + "... (42 more lines)\n"
    out = rendered(render_diff(diff, GLYPHS))
    assert "\r" not in out
    assert "\t" not in out
    assert "indented" in out
    assert "42 more lines" in out


def _additions_diff(count: int, name: str = "big.py") -> str:
    """A unified diff that adds *count* numbered lines to an empty file."""
    after = "".join(f"line {i}\n" for i in range(count))
    return make_diff("", after, name=name)


def test_render_diff_max_rows_param_exists() -> None:
    """CI guard: render_diff keeps its keyword-only max_rows parameter."""
    import inspect

    sig = inspect.signature(render_diff)
    assert "max_rows" in sig.parameters


def test_render_diff_max_rows_caps_output_with_footer() -> None:
    diff = _additions_diff(30)
    out = rendered(render_diff(diff, GLYPHS, max_rows=10))
    assert "line 0" in out
    assert "line 9" in out  # the 10th content row is shown
    assert "line 25" not in out  # rows past the cap are hidden
    assert f"{GLYPHS.ellipsis} (+" in out
    assert "more)" in out


def test_render_diff_max_rows_none_shows_full() -> None:
    diff = _additions_diff(30)
    out = rendered(render_diff(diff, GLYPHS, max_rows=None))
    assert "line 0" in out
    assert "line 29" in out
    assert "more)" not in out


def test_render_diff_max_rows_at_exact_boundary_no_footer() -> None:
    """rows == max_rows is inclusive: no cap, no footer."""
    diff = _additions_diff(8)
    # An 8-addition diff renders exactly 8 content rows.
    out = rendered(render_diff(diff, GLYPHS, max_rows=8))
    assert "line 7" in out
    assert "more)" not in out


def test_render_diff_footer_style_is_faint() -> None:
    from rich.console import Group
    from rich.text import Text

    diff = _additions_diff(30)
    panel = render_diff(diff, GLYPHS, max_rows=5)
    body = panel.renderable
    assert isinstance(body, Group)
    footer = body.renderables[-1]
    assert isinstance(footer, Text)
    assert footer.style == "sp.faint"
    assert footer.plain.startswith(f"{GLYPHS.ellipsis} (+")


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


def test_plan_panel_sanitizes_goal() -> None:
    """plan.goal is model-controlled; raw control chars must not reach the terminal."""
    plan = TaskPlan(
        task_id="20260611-031500-sec-test",
        goal="Safe goal\x1b[2Jforged\x00",
        user_intent="test",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[PlanStep(title="Only step")],
    )
    out = rendered(plan_panel(plan, GLYPHS))
    assert "\x1b" not in out
    assert "\x00" not in out
    assert "Safe goal" in out
    assert "forged" in out  # visible text preserved, only escape stripped


def test_plan_step_line_sanitizes_title() -> None:
    """step.title is model-controlled; raw escape sequences must not reach the terminal."""
    step = PlanStep(title="Compile\x1b[2Jspoof\x00", status="active")
    line = plan_step_line(1, step, GLYPHS)
    assert "\x1b" not in line.plain
    assert "\x00" not in line.plain
    assert "Compile" in line.plain
    assert "spoof" in line.plain  # visible text preserved


def test_plan_step_line_sanitizes_all_statuses() -> None:
    """Sanitization must fire for every branch in plan_step_line (completed/active/skipped/todo)."""
    poison = "\x1b[2Jx\x00"
    for status in ("completed", "active", "skipped", "pending"):
        step = PlanStep(title=f"Step{poison}", status=status if status != "pending" else "pending")
        line = plan_step_line(1, step, GLYPHS)
        assert "\x1b" not in line.plain, f"escape leaked for status={status!r}"
        assert "\x00" not in line.plain, f"null leaked for status={status!r}"
        assert "Step" in line.plain, f"visible text lost for status={status!r}"


def test_plan_panel_normal_goal_unchanged() -> None:
    """Sanitization must not alter clean goal text."""
    plan = TaskPlan(
        task_id="20260611-031500-normal",
        goal="Do the demo",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[PlanStep(title="Only step")],
    )
    out = rendered(plan_panel(plan, GLYPHS))
    assert "Goal: Do the demo" in out


def test_plan_step_line_normal_title_unchanged() -> None:
    """Sanitization must not alter clean step title text."""
    step = PlanStep(title="Run the tests", status="active")
    line = plan_step_line(1, step, GLYPHS)
    assert "Run the tests" in line.plain
