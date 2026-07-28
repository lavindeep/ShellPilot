"""Snapshot-style tests for the themed renderer components (design section 31)."""

from __future__ import annotations

import difflib
import io
from pathlib import Path

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from shellpilot.cli.render import (
    approval_choices,
    approval_cwd,
    approval_info,
    badge,
    context_line,
    output_truncation,
    plan_choices,
    plan_panel,
    plan_step_line,
    render_diff,
    tool_call,
    tool_call_block,
    tool_result,
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


def make_request(
    risk: RiskLevel = RiskLevel.MEDIUM,
    kind: str = "command",
    reasons: tuple[str, ...] = ("writes inside workspace",),
    purpose: str = "Writes the file.",
) -> ApprovalRequest:
    return ApprovalRequest(
        kind=kind,
        display="do the thing",
        risk=risk,
        reasons=reasons,
        purpose=purpose,
        cwd=Path("/tmp/ws"),
    )


def test_approval_card_binds_badge_stats_and_cwd_in_one_panel() -> None:
    from shellpilot.cli.render import approval_card

    card = approval_card(make_request())
    assert isinstance(card, Panel)
    out = rendered(card)
    assert "MEDIUM" in out
    assert "WHY" in out and "writes inside workspace" in out
    assert "EFFECT" in out and "Writes the file." in out
    assert "CWD" in out and "/tmp/ws" in out


def test_approval_card_border_carries_the_risk_color() -> None:
    from shellpilot.cli.render import approval_card

    medium = approval_card(make_request(risk=RiskLevel.MEDIUM))
    high = approval_card(make_request(risk=RiskLevel.HIGH))
    # A gated action is a decision point → amber border; HIGH escalates to red.
    assert str(medium.border_style) == "sp.warn"
    assert str(high.border_style) == "sp.error"


def test_approval_card_omits_empty_rows() -> None:
    from shellpilot.cli.render import approval_card

    card = approval_card(make_request(reasons=(), purpose=""))
    out = rendered(card)
    assert "WHY" not in out and "EFFECT" not in out
    assert "CWD" in out


def test_response_markdown_uses_ansi_code_theme() -> None:
    # Fenced code must render with ANSI colors on the terminal's own background
    # (§31.7) — never monokai's painted fill.
    from shellpilot.cli.render import response_markdown

    md = response_markdown("```python\nx = 1\n```")
    assert md.code_theme == "ansi_dark"


def test_response_markdown_sanitizes_control_chars() -> None:
    from shellpilot.cli.render import response_markdown

    md = response_markdown("hello\x00\x07 world")
    assert "\x00" not in md.markup and "\x07" not in md.markup
    assert "hello" in md.markup


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


def test_render_diff_width_standardizes_panel_width() -> None:
    """With width set, every diff renders to the SAME panel width regardless of
    its content width — a standardized diff window, not one hugging its longest
    line (§31.16)."""
    narrow = make_diff("a\n", "ab\n")
    wide = make_diff("a\n", "a" + "x" * 70 + "\n")
    for diff in (narrow, wide):
        out = rendered(render_diff(diff, GLYPHS, width=60), width=120)
        border = next(ln for ln in out.splitlines() if ln.startswith("╭"))
        assert len(border) == 60


def test_render_diff_width_wraps_long_line_onto_continuation() -> None:
    """A line wider than the panel folds onto continuation lines instead of
    running off the side; no rendered line exceeds the panel width and every
    character survives the wrap (§31.16)."""
    long = "x" * 90  # no spaces → must hard-fold, not word-wrap
    diff = make_diff("", long + "\n")
    lines = rendered(render_diff(diff, GLYPHS, width=50), width=120).splitlines()
    assert max(len(ln) for ln in lines) <= 50
    assert sum(ln.count("x") for ln in lines) == 90


def test_render_diff_width_fills_changed_bar_to_inner_width() -> None:
    """A short changed line's colored bar fills to the panel's inner width when a
    width is given, so bars stay full-width at the standardized size (§31.4)."""
    from shellpilot.cli.render import _diff_rows

    diff = make_diff("a\n", "b\n")
    rows, _ = _diff_rows(diff, GLYPHS, width=60)
    add_row = next(r for r in rows if "+ " in r.plain)
    end = next(span.end for span in add_row.spans if span.style == "sp.diff.add")
    assert end == 60 - 4  # styled bar reaches the inner width (panel - borders - padding)


def test_badge_chips_and_plain_degradation() -> None:
    assert badge("medium").plain == " MEDIUM "
    assert badge("high").plain == " HIGH "
    assert badge("blocked", plain=True).plain == "[BLOCKED]"


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


# --- Approval restyle (design §31.5): commands/descriptions/choices must not be gray ---


def _styles(text: object) -> set[str]:
    return {span.style for span in text.spans}  # type: ignore[attr-defined]


def _req(
    risk: RiskLevel,
    *,
    kind: str = "command",
    reasons: tuple[str, ...] = ("recursive delete",),
    purpose: str = "",
    cwd: str = "/tmp/ws",
) -> ApprovalRequest:
    return ApprovalRequest(
        kind=kind,
        display="cmd",
        risk=risk,
        reasons=reasons,
        cwd=Path(cwd),
        purpose=purpose,
    )


def test_tool_call_generic_args_stay_dim() -> None:
    """The generic fallback line dims its args (subject tools route via block)."""
    other = tool_call("env_info", "verbose=True", GLYPHS)
    assert "sp.cmd" not in _styles(other)
    assert "sp.dim" in _styles(other)


def _identity(path: str) -> str:
    return path


def _block_text(block: list[RenderableType]) -> str:
    return "\n".join(rendered(r) for r in block)


def _line(block: list[RenderableType]) -> Text:
    assert isinstance(block[0], Text)
    return block[0]


def test_tool_call_block_frames_run_command() -> None:
    """run_command shows the action label + the actual command framed and bright,
    never the run_command(argv=[...]) repr."""
    block = tool_call_block(
        "run_command", {"argv": ["pytest", "-q"]}, GLYPHS, path_display=_identity
    )
    assert len(block) == 2
    header, frame = block
    assert isinstance(header, Text) and isinstance(frame, Panel)
    assert "run command" in header.plain
    assert "argv" not in header.plain and "(" not in header.plain
    assert "sp.emph" in _styles(header)
    inner = frame.renderable
    assert isinstance(inner, Text)
    assert inner.plain == "pytest -q"
    assert inner.style == "sp.cmd"  # bright, impossible to miss


def test_tool_call_block_frames_web_fetch_url() -> None:
    block = tool_call_block(
        "web_fetch", {"url": "https://python.org/downloads"}, GLYPHS, path_display=_identity
    )
    assert len(block) == 2
    assert "web_fetch" in _line(block).plain
    assert isinstance(block[1], Panel)
    assert "https://python.org/downloads" in _block_text(block)


def test_tool_call_block_inline_subject_for_reads() -> None:
    """A read tool shows `⏺ name  subject` with a clean, readable subject."""
    block = tool_call_block("read_file", {"path": "client.py"}, GLYPHS, path_display=_identity)
    assert len(block) == 1
    line = _line(block)
    assert f"{GLYPHS.bullet} read_file" in line.plain
    assert "client.py" in line.plain
    assert "path=" not in line.plain and "(" not in line.plain
    assert "sp.value" in _styles(line)
    assert "sp.emph" in _styles(line)  # the name


def test_tool_call_block_inline_subject_query_and_pattern() -> None:
    web = tool_call_block(
        "web_search", {"query": "py 3.13 release"}, GLYPHS, path_display=_identity
    )
    assert "py 3.13 release" in _line(web).plain and "query=" not in _line(web).plain
    grep = tool_call_block(
        "search_text", {"pattern": "TODO", "path": "src"}, GLYPHS, path_display=_identity
    )
    assert "TODO" in _line(grep).plain and "pattern=" not in _line(grep).plain
    skill = tool_call_block(
        "skill_read", {"skill": "git", "resource": "rebase"}, GLYPHS, path_display=_identity
    )
    assert "rebase" in _line(skill).plain


def test_tool_call_block_resolves_path_via_path_display() -> None:
    """A path subject is the resolved, workspace-relative target — never the raw
    (possibly spoofing) arg (§14.5)."""
    block = tool_call_block(
        "read_file", {"path": "notes/../secret.txt"}, GLYPHS, path_display=lambda _p: "secret.txt"
    )
    assert "secret.txt" in _line(block).plain
    assert "notes/../secret.txt" not in _line(block).plain


def test_tool_call_block_generic_fallback_for_unknown_tool() -> None:
    block = tool_call_block("env_info", {"verbose": True}, GLYPHS, path_display=_identity)
    assert len(block) == 1
    assert "env_info(" in _line(block).plain
    assert "verbose=True" in _line(block).plain


def test_tool_call_block_empty_argv_falls_back_to_generic() -> None:
    block = tool_call_block("run_command", {"argv": []}, GLYPHS, path_display=_identity)
    assert len(block) == 1  # no subject → generic, no frame


def test_tool_call_block_redacts_and_sanitizes_subject() -> None:
    block = tool_call_block("read_file", {"path": "a\x00b\x1b[2Jc"}, GLYPHS, path_display=_identity)
    text = _block_text(block)
    assert not any(c in text for c in "\x00\x1b")


def test_approval_info_stat_block_labels_and_bright_values() -> None:
    info = approval_info(
        _req(RiskLevel.HIGH, reasons=("recursive delete",), purpose="deletes files permanently")
    )
    assert "HIGH" in info.plain  # colored badge
    assert "recursive delete" in info.plain  # the why
    assert "deletes files permanently" in info.plain  # the effect/purpose
    styles = _styles(info)
    assert "sp.value" in styles  # values are readable, not dim
    assert "sp.label" in styles  # labels are muted


def test_approval_info_sanitizes_reason_and_purpose() -> None:
    info = approval_info(
        _req(RiskLevel.HIGH, reasons=("recur\x1b[2Jsive\x00",), purpose="del\x1betes")
    )
    assert "\x1b" not in info.plain
    assert "\x00" not in info.plain


def test_approval_cwd_labeled_readable_value() -> None:
    info = approval_cwd(_req(RiskLevel.MEDIUM, cwd="/tmp/ws"))
    assert "/tmp/ws" in info.plain
    assert "CWD" in info.plain
    assert "sp.value" in _styles(info)


def test_approval_choices_colored_yes_edit_no() -> None:
    ch = approval_choices(_req(RiskLevel.MEDIUM))
    assert "[y]es / [e]dit / [n]o" in ch.plain
    styles = _styles(ch)
    assert "sp.choice.yes" in styles
    assert "sp.choice.edit" in styles
    assert "sp.choice.no" in styles


def test_approval_choices_high_command_requires_run() -> None:
    ch = approval_choices(_req(RiskLevel.HIGH, kind="command"))
    assert "run" in ch.plain
    assert "[y]es" not in ch.plain
    styles = _styles(ch)
    assert "sp.risk.high" in styles  # the typed-run confirm is red
    assert "sp.choice.edit" in styles


def test_approval_choices_high_tool_uses_yes_no() -> None:
    """A HIGH-risk sensitive READ (kind=tool) keeps the y/e/n prompt, not typed-run."""
    ch = approval_choices(_req(RiskLevel.HIGH, kind="tool"))
    assert "[y]es / [e]dit / [n]o" in ch.plain
    assert "sp.choice.yes" in _styles(ch)


def test_plan_choices_colored() -> None:
    ch = plan_choices()
    assert "[y]es / [e]dit / [n]o" in ch.plain
    assert {"sp.choice.yes", "sp.choice.edit", "sp.choice.no"} <= _styles(ch)
