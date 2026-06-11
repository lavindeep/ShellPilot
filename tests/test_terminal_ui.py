"""Tests for the themed TerminalUI (design section 31.5/31.6)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

from rich.console import Console

from shellpilot.cli.terminal import TerminalUI
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan

GLYPHS = UNICODE_GLYPHS


def make_console() -> Console:
    return Console(
        record=True,
        width=100,
        file=io.StringIO(),
        theme=SHELLPILOT_THEME,
        force_terminal=True,
    )


def make_ui(console: Console, answers: list[str]) -> TerminalUI:
    ui = TerminalUI(console, glyphs=GLYPHS, spinner=False)
    answer_iter: Iterator[str] = iter(answers)

    def fake_input(prompt: str = "", **kwargs: object) -> str:
        console.print(prompt, end="")  # echo like the real Console.input
        return next(answer_iter)

    console.input = fake_input  # type: ignore[method-assign]
    return ui


def medium_request(diff: str = "") -> ApprovalRequest:
    return ApprovalRequest(
        kind="tool",
        display="patch_file hello.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
        diff=diff,
    )


def high_request() -> ApprovalRequest:
    return ApprovalRequest(
        kind="command",
        display="rm -rf build/",
        risk=RiskLevel.HIGH,
        reasons=("recursive delete",),
        cwd=Path("/tmp/ws"),
        purpose="Removes stale build output.",
    )


def test_medium_approval_accepts_yes_case_insensitively() -> None:
    console = make_console()
    assert make_ui(console, ["Y"]).ask_approval(medium_request()) is True
    out = console.export_text()
    assert " MEDIUM " in out
    assert "[y/n]" in out
    assert "[y/N]" not in out


def test_medium_approval_enter_defaults_to_no() -> None:
    console = make_console()
    assert make_ui(console, [""]).ask_approval(medium_request()) is False


def test_high_approval_requires_typed_run() -> None:
    console = make_console()
    assert make_ui(console, ["y"]).ask_approval(high_request()) is False
    console2 = make_console()
    assert make_ui(console2, ["run"]).ask_approval(high_request()) is True
    out = console2.export_text()
    assert " HIGH " in out
    assert "Removes stale build output." in out


def test_approval_renders_diff_panel() -> None:
    diff = '--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-print("done")\n+print("Goodbye World")\n'
    console = make_console()
    make_ui(console, ["y"]).ask_approval(medium_request(diff=diff))
    out = console.export_text()
    assert "hello.py" in out
    assert '+ print("Goodbye World")' in out
    assert "╭" in out  # panel, not raw diff text


def plan() -> TaskPlan:
    return TaskPlan(
        task_id="20260611-040000-demo",
        goal="Demo goal",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[PlanStep(title="First", status="completed"), PlanStep(title="Second")],
    )


def test_plan_approval_panel_and_choices() -> None:
    console = make_console()
    ui = make_ui(console, ["y"])
    assert ui.ask_plan_approval(plan(), "/tmp/ws/PLAN.md") == ("y", "")
    out = console.export_text()
    assert "Plan · 20260611-040000-demo" in out
    assert "Goal: Demo goal" in out
    assert "/tmp/ws/PLAN.md" in out


def test_plan_approval_edit_collects_revision() -> None:
    console = make_console()
    ui = make_ui(console, ["e", "add a verification step"])
    assert ui.ask_plan_approval(plan(), "p") == ("e", "add a verification step")


def test_show_plan_progress_prints_checklist() -> None:
    console = make_console()
    ui = make_ui(console, [])
    ui.show_plan_progress(plan())
    out = console.export_text()
    assert f"{GLYPHS.check} 1" in out
    assert f"{GLYPHS.todo} 2" in out


def test_tool_call_and_result_lines() -> None:
    console = make_console()
    ui = make_ui(console, [])
    ui.show_tool_call("patch_file", {"path": "hello.py"})
    ui.show_tool_result("patch_file", True, "1 addition")
    out = console.export_text()
    assert f"{GLYPHS.bullet} patch_file" in out
    assert f"{GLYPHS.check} 1 addition" in out


# ---------------------------------------------------------------------------
# A5: show_plan_progress ends with a blank line
# ---------------------------------------------------------------------------


def test_show_plan_progress_ends_with_blank_line() -> None:
    """show_plan_progress must append a blank line so the checklist is visually
    separated from the streamed response that follows."""
    console = make_console()
    ui = make_ui(console, [])
    ui.show_plan_progress(plan())
    raw = console.export_text(clear=False)
    # The exported text should end with two newlines (last content line + blank)
    assert raw.endswith("\n\n"), repr(raw[-20:])
