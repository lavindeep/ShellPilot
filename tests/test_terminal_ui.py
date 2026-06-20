"""Tests for the themed TerminalUI (design section 31.5/31.6)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

from shellpilot.cli.terminal import TerminalUI, should_discard_interrupt
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.memory.redaction import REDACTED
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan

GLYPHS = UNICODE_GLYPHS


def make_console() -> Console:
    # no_color=False explicitly overrides the NO_COLOR env var so badge colour
    # assertions are reliable regardless of the test environment.
    return Console(
        record=True,
        width=100,
        file=io.StringIO(),
        theme=SHELLPILOT_THEME,
        force_terminal=True,
        no_color=False,
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


@pytest.mark.parametrize("method_name", ["show_status", "show_error", "show_command_output"])
def test_terminal_text_sinks_sanitize_external_text(method_name: str) -> None:
    console = make_console()
    ui = make_ui(console, [])

    getattr(ui, method_name)("visible\x1b[2J\x00\ttext\x07\x7f")

    out = console.export_text()
    assert not any(char in out for char in "\x1b\x00\x07\x7f\t")
    assert "visible" in out and "text" in out


# ---------------------------------------------------------------------------
# Fix 2: show_tool_call redacts secrets in the summary display line
# ---------------------------------------------------------------------------


def test_show_tool_call_redacts_secret_in_summary() -> None:
    """show_tool_call must not print raw secrets in the summary line."""
    console = make_console()
    ui = make_ui(console, [])
    # ghp_ token matches the GitHub classic token pattern in redaction.py
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    ui.show_tool_call("run_command", {"token": secret})
    out = console.export_text()
    assert secret not in out
    assert REDACTED in out


def test_show_tool_call_plain_argument_unchanged() -> None:
    """A non-secret argument must render unchanged."""
    console = make_console()
    ui = make_ui(console, [])
    ui.show_tool_call("read_file", {"path": "/tmp/notes.txt"})
    out = console.export_text()
    assert "/tmp/notes.txt" in out


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


# ---------------------------------------------------------------------------
# A10: per-tool spinner label tests
# ---------------------------------------------------------------------------


class _RecordingSpinner:
    """Minimal spinner double that records start/stop calls."""

    def __init__(self) -> None:
        self.started_labels: list[str | None] = []
        self.stops: int = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, label: object = None) -> None:
        self._active = True
        self.started_labels.append(str(label) if label is not None else None)

    def stop(self) -> None:
        self._active = False
        self.stops += 1


def _ui_with_recording_spinner(
    console: Console, answers: list[str]
) -> tuple[TerminalUI, _RecordingSpinner]:
    """Return a TerminalUI whose spinner is replaced by a _RecordingSpinner."""
    ui = make_ui(console, answers)
    spy = _RecordingSpinner()
    ui._spinner = spy  # type: ignore[assignment]
    return ui, spy


def test_tool_call_starts_labeled_spinner_and_result_stops_it() -> None:
    """show_tool_call starts the spinner with a label; show_tool_result stops it."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])

    ui.show_tool_call("patch_file", {"path": "hello.py"})
    assert len(spy.started_labels) == 1
    label = spy.started_labels[0]
    assert label is not None
    assert "patch_file" in label

    ui.show_tool_result("patch_file", True, "ok")
    assert spy.stops >= 1


def test_tool_call_sanitizes_spinner_label() -> None:
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])

    ui.show_tool_call("visible\x1b[2J\x00text\x07", {})

    assert spy.started_labels == ["running visible[2Jtext"]


def test_approval_stops_spinner_before_input() -> None:
    """ask_approval stops the spinner before prompting the user."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, ["y"])
    spy._active = True  # pretend the spinner is running

    ui.ask_approval(medium_request())
    assert spy.stops >= 1


def test_show_error_stops_spinner() -> None:
    """show_error stops the spinner before printing."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_error("something went wrong")
    assert spy.stops >= 1


def test_show_command_output_stops_spinner() -> None:
    """show_command_output stops the spinner before printing output."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_command_output("line of output")
    assert spy.stops >= 1


def test_show_plan_progress_stops_spinner() -> None:
    """show_plan_progress stops the spinner before printing the checklist."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_plan_progress(plan())
    assert spy.stops >= 1


# ---------------------------------------------------------------------------
# Fix 1: should_discard_interrupt timing window
# ---------------------------------------------------------------------------


def test_discard_stale_interrupt_within_window() -> None:
    """A Ctrl-C arriving 0.01 s after a turn just ran should be discarded."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.01) is True


def test_do_not_discard_genuine_late_interrupt() -> None:
    """A Ctrl-C arriving 0.5 s after a turn should NOT be discarded."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.5) is False


def test_do_not_discard_interrupt_at_idle_prompt() -> None:
    """A Ctrl-C at the prompt with no recent turn should NOT be discarded."""
    assert should_discard_interrupt(turn_just_ran=False, elapsed_seconds=0.01) is False


def test_discard_at_exact_boundary_is_false() -> None:
    """The boundary (elapsed == window) is exclusive: do NOT discard at exactly window_seconds."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.1) is False


def test_custom_window_seconds() -> None:
    """The window_seconds parameter is respected."""
    assert (
        should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.04, window_seconds=0.05)
        is True
    )
    assert (
        should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.06, window_seconds=0.05)
        is False
    )
