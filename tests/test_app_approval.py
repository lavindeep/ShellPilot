"""Tests for the approval focus-swap gate (design section 31.16).

The pure parse helpers carry the three-way y/e/n + HIGH typed-"run" contract and
are tested directly. The gate handshake is exercised across a real worker thread
(the worker blocks on a ``concurrent.futures.Future`` while the test thread acts
as the loop thread: it drains the scheduled ``_enter`` thunk, then submits keys).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from shellpilot.cli.app_approval import (
    NEED_STEER,
    ApprovalGate,
    parse_command_choice,
    parse_plan_choice,
)
from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.theme import UNICODE_GLYPHS
from shellpilot.policy.approvals import APPROVE, DECLINE, ApprovalReply, ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan


def high_command() -> ApprovalRequest:
    return ApprovalRequest(
        kind="command",
        display="rm -rf build",
        risk=RiskLevel.HIGH,
        reasons=("recursive delete",),
        cwd=Path("/tmp/ws"),
    )


def normal_tool() -> ApprovalRequest:
    return ApprovalRequest(
        kind="tool",
        display="patch_file x.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
    )


def sensitive_read() -> ApprovalRequest:
    return ApprovalRequest(
        kind="tool",
        display="read_file ~/.ssh/id",
        risk=RiskLevel.HIGH,
        reasons=("sensitive read",),
        cwd=Path("/tmp/ws"),
    )


def make_plan() -> TaskPlan:
    return TaskPlan(
        task_id="20260611-040000-demo",
        goal="Demo goal",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[PlanStep(title="First"), PlanStep(title="Second")],
    )


# --- Pure parse helpers (no threading) ---------------------------------------


def test_parse_command_choice_high_command() -> None:
    req = high_command()
    assert parse_command_choice(req, "run") == APPROVE
    assert parse_command_choice(req, "  RUN ") == APPROVE  # trimmed + case-folded
    assert parse_command_choice(req, "e") is NEED_STEER
    assert parse_command_choice(req, "edit") is NEED_STEER
    # Only the literal "run" executes a HIGH command; y/yes do NOT.
    assert parse_command_choice(req, "y") == DECLINE
    assert parse_command_choice(req, "yes") == DECLINE
    assert parse_command_choice(req, "x") == DECLINE
    assert parse_command_choice(req, "") == DECLINE


def test_parse_command_choice_normal() -> None:
    req = normal_tool()
    assert parse_command_choice(req, "y") == APPROVE
    assert parse_command_choice(req, "yes") == APPROVE
    assert parse_command_choice(req, "e") is NEED_STEER
    assert parse_command_choice(req, "edit") is NEED_STEER
    assert parse_command_choice(req, "n") == DECLINE
    assert parse_command_choice(req, "x") == DECLINE
    assert parse_command_choice(req, "") == DECLINE


def test_parse_command_choice_high_tool_uses_yen_path() -> None:
    # A HIGH-risk TOOL is a sensitive read, not a command — it takes the y/e/n
    # path, not the typed-"run" path.
    req = sensitive_read()
    assert parse_command_choice(req, "y") == APPROVE
    assert parse_command_choice(req, "run") == DECLINE  # "run" is not y/yes here
    assert parse_command_choice(req, "e") is NEED_STEER


def test_parse_plan_choice() -> None:
    assert parse_plan_choice("y") == "y"
    assert parse_plan_choice("yes") == "y"
    assert parse_plan_choice("e") == "e"
    assert parse_plan_choice("edit") == "e"
    assert parse_plan_choice("n") == "n"
    assert parse_plan_choice("no") == "n"
    assert parse_plan_choice("") == "n"  # empty → decline, mirrors TerminalUI
    assert parse_plan_choice("x") is None  # unrecognized → re-prompt


# --- Gate handshake across a real worker thread ------------------------------


def make_gate() -> tuple[ApprovalGate, list[object]]:
    sink: list[object] = []
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    gate = ApprovalGate(ui=ui, schedule=sink.append, glyphs=UNICODE_GLYPHS)
    return gate, sink


def drain_enter(sink: list[object], timeout: float = 2.0) -> None:
    """Busy-wait until the worker scheduled its _enter thunk, then run it here.

    Running the thunk on the test thread plays the role of the loop thread: it
    renders the prompt and arms gate._pending.
    """
    deadline = time.monotonic() + timeout
    while not sink and time.monotonic() < deadline:
        time.sleep(0.001)
    assert sink, "worker never scheduled the enter thunk"
    thunk = sink.pop(0)
    assert callable(thunk)
    thunk()


def run_command(
    gate: ApprovalGate, request: ApprovalRequest
) -> tuple[threading.Thread, list[object]]:
    holder: list[object] = []
    # daemon=True so a regression that fails to resolve the Future fails the test
    # FAST (join times out → assert not is_alive) instead of wedging the suite at
    # process exit on a blocked non-daemon worker.
    thread = threading.Thread(target=lambda: holder.append(gate.ask_command(request)), daemon=True)
    thread.start()
    return thread, holder


def run_plan(
    gate: ApprovalGate, plan: TaskPlan, path: str
) -> tuple[threading.Thread, list[object]]:
    holder: list[object] = []
    thread = threading.Thread(target=lambda: holder.append(gate.ask_plan(plan, path)), daemon=True)
    thread.start()
    return thread, holder


def test_gate_command_run_approves() -> None:
    gate, sink = make_gate()
    thread, holder = run_command(gate, high_command())
    drain_enter(sink)
    assert gate.active
    gate.submit("run")
    thread.join(2.0)
    assert not thread.is_alive()
    assert holder == [APPROVE]
    assert not gate.active


def test_gate_command_decline() -> None:
    gate, sink = make_gate()
    thread, holder = run_command(gate, high_command())
    drain_enter(sink)
    gate.submit("")  # Enter → cancel a HIGH command
    thread.join(2.0)
    assert holder == [DECLINE]


def test_gate_command_edit_then_steer() -> None:
    gate, sink = make_gate()
    thread, holder = run_command(gate, high_command())
    drain_enter(sink)
    gate.submit("e")  # enter steer phase — does NOT resolve
    assert gate.active
    assert holder == []
    gate.submit("do X instead")
    thread.join(2.0)
    assert holder == [ApprovalReply(approved=False, steer_text="do X instead")]


def test_gate_command_empty_steer_is_plain_decline() -> None:
    gate, sink = make_gate()
    thread, holder = run_command(gate, high_command())
    drain_enter(sink)
    gate.submit("e")
    gate.submit("   ")  # whitespace-only steer → plain decline (steer_text None)
    thread.join(2.0)
    assert holder == [ApprovalReply(approved=False, steer_text=None)]


def test_gate_command_cancel_declines() -> None:
    gate, sink = make_gate()
    thread, holder = run_command(gate, high_command())
    drain_enter(sink)
    gate.cancel()  # Ctrl-C during approval → decline THIS action
    thread.join(2.0)
    assert holder == [DECLINE]
    assert not gate.active


def test_gate_plan_yes() -> None:
    gate, sink = make_gate()
    thread, holder = run_plan(gate, make_plan(), "/tmp/ws/PLAN.md")
    drain_enter(sink)
    gate.submit("y")
    thread.join(2.0)
    assert holder == [("y", "")]


def test_gate_plan_edit_then_revision() -> None:
    gate, sink = make_gate()
    thread, holder = run_plan(gate, make_plan(), "/tmp/ws/PLAN.md")
    drain_enter(sink)
    gate.submit("e")
    assert gate.active
    gate.submit("make it shorter")
    thread.join(2.0)
    assert holder == [("e", "make it shorter")]


def test_gate_plan_unrecognized_reprompts_then_resolves() -> None:
    gate, sink = make_gate()
    thread, holder = run_plan(gate, make_plan(), "/tmp/ws/PLAN.md")
    drain_enter(sink)
    gate.submit("x")  # unrecognized → re-prompt, stays active
    assert gate.active
    assert holder == []
    gate.submit("y")
    thread.join(2.0)
    assert holder == [("y", "")]


def test_gate_plan_cancel_declines() -> None:
    gate, sink = make_gate()
    thread, holder = run_plan(gate, make_plan(), "/tmp/ws/PLAN.md")
    drain_enter(sink)
    gate.cancel()
    thread.join(2.0)
    assert holder == [("n", "")]


# --- Fail-closed: a render/echo sink raising must NOT wedge the worker ---------


class _BoomUI:
    """AppUI stand-in whose chosen sink raises, to prove the gate fails closed.

    ``boom_on="approval"`` raises in the setup render (``_enter_*``); ``"echo"``
    lets setup succeed (the choice line goes through ``show_choices``, not
    ``show_status``) then raises on the echo — the first ``show_status``, inside
    ``feed`` before the Future is resolved.
    """

    def __init__(self, *, boom_on: str) -> None:
        self.boom_on = boom_on
        self._status_calls = 0

    def show_approval(self, request: object) -> None:
        if self.boom_on == "approval":
            raise RuntimeError("boom-approval")

    def show_plan_approval(self, plan: object, path: str) -> None:
        if self.boom_on == "approval":
            raise RuntimeError("boom-approval")

    def show_choices(self, choices: object) -> None:
        # The styled choice line; setup must succeed for the "echo" case.
        return None

    def show_status(self, text: str) -> None:
        self._status_calls += 1
        if self.boom_on == "echo" and self._status_calls >= 1:
            raise RuntimeError("boom-echo")


def _run_command_capturing(gate: ApprovalGate) -> tuple[threading.Thread, list[BaseException]]:
    errs: list[BaseException] = []

    def worker() -> None:
        try:
            gate.ask_command(high_command())
        except BaseException as exc:  # noqa: BLE001 - capture the forwarded failure
            errs.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, errs


def test_gate_setup_exception_resolves_future_not_hang() -> None:
    # show_approval raises during _enter_command → the Future is resolved with the
    # error (set_exception) and the worker unblocks, instead of hanging on result().
    sink: list[object] = []
    gate = ApprovalGate(ui=_BoomUI(boom_on="approval"), schedule=sink.append, glyphs=UNICODE_GLYPHS)  # type: ignore[arg-type]
    thread, errs = _run_command_capturing(gate)
    drain_enter(sink)
    thread.join(2.0)
    assert not thread.is_alive()  # did NOT wedge
    assert errs and "boom-approval" in str(errs[0])
    assert not gate.active


def test_gate_feed_exception_resolves_future_not_hang() -> None:
    # The echo sink raises inside feed (before set_result) → submit forwards the
    # error to the Future; the worker unblocks rather than hanging.
    sink: list[object] = []
    gate = ApprovalGate(ui=_BoomUI(boom_on="echo"), schedule=sink.append, glyphs=UNICODE_GLYPHS)  # type: ignore[arg-type]
    thread, errs = _run_command_capturing(gate)
    drain_enter(sink)
    assert gate.active
    gate.submit("run")
    thread.join(2.0)
    assert not thread.is_alive()
    assert errs and "boom-echo" in str(errs[0])
    assert not gate.active
