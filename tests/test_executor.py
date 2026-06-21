"""Tests for the tool broker recovery loop with the fake model (section 10.4)."""

from pathlib import Path
from typing import Any

import pytest

from shellpilot.config.model import RuntimeSettings, Settings
from shellpilot.llm.messages import ToolCall, ToolDefinition
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.policy.approvals import APPROVE, DECLINE, ApprovalReply, ApprovalRequest
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.executor import ToolExecutor
from shellpilot.tools.base import ToolResult, ToolSpec
from shellpilot.tools.registry import ToolRegistry
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI

# ---------------------------------------------------------------------------
# Helpers shared by executor unit tests
# ---------------------------------------------------------------------------


def _make_spec(
    name: str = "dummy",
    precheck: Any = None,
) -> ToolSpec:
    """Build a minimal ToolSpec for executor-level unit tests."""
    return ToolSpec(
        definition=ToolDefinition(
            name=name,
            description="test tool",
            parameters={"x": {"type": "string"}},
            required=("x",),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=lambda ctx, args: ToolResult(success=True, summary="ok", content=""),
        precheck=precheck,
    )


def _make_executor(
    spec: ToolSpec,
    tmp_path: Path,
    ask_approval: Any = None,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=ask_approval,
    )


def make_runtime(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, settings: Settings | None = None
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )


def test_tool_call_round_trip(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("remember the milk")
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="notes.txt"),
            answer("Your note says: remember the milk."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("What is in notes.txt?")

    assert reply == "Your note says: remember the milk."
    assert ui.tool_calls == [("read_file", {"path": "notes.txt"})]
    assert ui.tool_results[0][1] is True
    # The model received the tool result back.
    final_call = fake.calls[-1]
    tool_messages = [m for m in final_call.messages if m.role == "tool"]
    assert any("remember the milk" in m.content for m in tool_messages)


def test_unknown_tool_gets_reminder_then_recovers(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("read_files", path="x"),  # malformed: unknown tool
            answer("Recovered without tools."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("read x")

    assert reply == "Recovered without tools."
    final_call = fake.calls[-1]
    tool_messages = [m for m in final_call.messages if m.role == "tool"]
    assert any("unknown tool" in m.content for m in tool_messages)
    assert any("Retry once" in m.content for m in tool_messages)


def test_invalid_args_get_schema_reminder(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("read_file", file="x.txt"),  # malformed: wrong arg name
            answer("done"),
        ]
    )
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    runtime.run_turn("read")
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("read_file(" in m.content for m in tool_messages)  # schema reminder


def test_two_consecutive_malformed_calls_stop_tool_use(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("bogus_one"),
            tool_call("bogus_two"),
            answer("Stopping cleanly."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("do something")

    assert reply == "Stopping cleanly."
    assert any("malformed" in status.lower() for status in ui.statuses)
    # After the stop, the final call must offer no tools.
    assert fake.calls[-1].tools == ()


def test_tool_loop_budget_stops_runaway_model(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("data")
    max_turns = 3
    settings = Settings(runtime=RuntimeSettings(max_tool_turns=max_turns))
    script = [tool_call("read_file", path="f.txt") for _ in range(max_turns + 1)]
    script.append(answer("Finished after budget stop."))
    fake = FakeLLM(script=script)
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path, settings)

    reply = runtime.run_turn("loop forever")

    assert reply == "Finished after budget stop."
    assert any("budget" in status.lower() for status in ui.statuses)
    assert fake.calls[-1].tools == ()


def test_tool_crash_is_reported_not_raised(tmp_path: Path) -> None:
    # read_file on a directory path that exists triggers the not-a-file branch;
    # simulate a crash instead via an unreadable argument type accepted by schema.
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="missing-file.txt"),
            answer("Handled the failure."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)
    reply = runtime.run_turn("read it")
    assert reply == "Handled the failure."
    assert ui.tool_results[0][1] is False  # failure surfaced to the user
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("status: failed" in m.content for m in tool_messages)


# ---------------------------------------------------------------------------
# ToolExecutor precheck unit tests
# ---------------------------------------------------------------------------


def test_precheck_failure_returns_failed_result_without_approval(tmp_path: Path) -> None:
    """Precheck returning a message produces a standard failed result; the approval
    asker must never be invoked."""
    approval_called = False

    def _never_ask(request: Any) -> Any:
        nonlocal approval_called
        approval_called = True
        pytest.fail("approval asker must not be called when precheck fails")

    spec = _make_spec(
        name="dummy",
        precheck=lambda ctx, args: "precheck rejection message",
    )
    executor = _make_executor(spec, tmp_path, ask_approval=_never_ask)
    call = ToolCall(name="dummy", arguments={"x": "val"})
    outcome = executor.execute(call)

    assert not outcome.malformed
    assert outcome.result is not None
    assert not outcome.result.success
    assert "precheck rejection message" in outcome.result.summary
    # Standard failed rendering: tool: <name>\nstatus: failed\nsummary: <msg>
    assert "tool: dummy" in outcome.model_text
    assert "status: failed" in outcome.model_text
    assert "precheck rejection message" in outcome.model_text
    # Must NOT be the BLOCK path (no "Do not retry this action.")
    assert "Do not retry this action." not in outcome.model_text
    # Must NOT be malformed (no schema reminder)
    assert "Schema:" not in outcome.model_text
    assert not approval_called


def test_precheck_pass_proceeds_to_handler(tmp_path: Path) -> None:
    """A precheck that returns None lets the call proceed normally."""
    spec = _make_spec(
        name="dummy",
        precheck=lambda ctx, args: None,
    )
    executor = _make_executor(spec, tmp_path)
    call = ToolCall(name="dummy", arguments={"x": "val"})
    outcome = executor.execute(call)

    assert not outcome.malformed
    assert outcome.result is not None
    assert outcome.result.success


def test_no_precheck_proceeds_normally(tmp_path: Path) -> None:
    """When precheck is None (default), the call proceeds normally."""
    spec = _make_spec(name="dummy", precheck=None)
    executor = _make_executor(spec, tmp_path)
    call = ToolCall(name="dummy", arguments={"x": "val"})
    outcome = executor.execute(call)

    assert outcome.result is not None
    assert outcome.result.success


# ---------------------------------------------------------------------------
# Deterministic contract validation tests (v0.5.2 — enum, array items, bounds)
# These all route through the EXISTING malformed-call path: validate_args
# returns an error string → executor wraps it in model_text with schema
# reminder → outcome.malformed is True → ConversationRuntime increments
# consecutive_malformed and sends a schema-reminder message.
# ---------------------------------------------------------------------------


def _make_spec_with_schema(
    name: str,
    parameters: dict[str, Any],
    required: tuple[str, ...] = (),
) -> ToolSpec:
    """Build a ToolSpec with a custom parameter schema for validation tests."""
    return ToolSpec(
        definition=ToolDefinition(
            name=name,
            description="test tool",
            parameters=parameters,
            required=required,
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=lambda ctx, args: ToolResult(success=True, summary="ok", content=""),
    )


def _make_executor_direct(spec: ToolSpec, tmp_path: Path) -> ToolExecutor:
    """ToolExecutor for direct execute() calls (no approval needed)."""
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )


# --- patch_file.operation enum ---


def test_patch_file_bad_operation_is_malformed(tmp_path: Path) -> None:
    """patch_file with operation='insert' is rejected via the malformed path."""
    from shellpilot.tools.patch import OPERATIONS, PATCH_FILE

    registry = ToolRegistry()
    registry.register(PATCH_FILE)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )
    call = ToolCall(
        name="patch_file",
        arguments={"path": "f.txt", "operation": "insert", "old": "x"},
    )
    outcome = executor.execute(call)

    assert outcome.malformed
    assert outcome.result is None
    # Error message must name the parameter and list the allowed operations.
    assert "operation" in outcome.model_text
    for op in OPERATIONS:
        assert op in outcome.model_text, f"expected '{op}' in error message"
    # Schema reminder fires (malformed path).
    assert "patch_file(" in outcome.model_text


def test_patch_file_valid_operation_passes(tmp_path: Path) -> None:
    """Regression: patch_file with a valid operation is not rejected at validation."""
    from shellpilot.tools.base import validate_args
    from shellpilot.tools.patch import PATCH_FILE

    error = validate_args(PATCH_FILE, {"path": "f.txt", "operation": "replace_exact", "old": "x"})
    assert error is None


# --- write_file.mode enum ---


def test_write_file_bad_mode_is_malformed(tmp_path: Path) -> None:
    """write_file with mode='replace' is rejected via the malformed path."""
    from shellpilot.tools.patch import WRITE_FILE, WRITE_MODES

    registry = ToolRegistry()
    registry.register(WRITE_FILE)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )
    call = ToolCall(
        name="write_file",
        arguments={"path": "f.txt", "content": "hello", "mode": "replace"},
    )
    outcome = executor.execute(call)

    assert outcome.malformed
    assert outcome.result is None
    assert "mode" in outcome.model_text
    for mode in WRITE_MODES:
        assert mode in outcome.model_text, f"expected '{mode}' in error message"
    assert "write_file(" in outcome.model_text


def test_write_file_valid_mode_passes(tmp_path: Path) -> None:
    """Regression: write_file with a valid mode is not rejected at validation."""
    from shellpilot.tools.base import validate_args
    from shellpilot.tools.patch import WRITE_FILE

    error = validate_args(WRITE_FILE, {"path": "f.txt", "content": "x", "mode": "create"})
    assert error is None


# --- update_plan.status enum ---


def test_update_plan_bad_status_is_malformed(tmp_path: Path) -> None:
    """update_plan with status='finished' is rejected via the malformed path."""
    from shellpilot.runtime.planner import STEP_STATUSES, PlanManager, make_plan_tools

    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=lambda plan, path: ("n", ""),
        get_user_intent=lambda: "intent",
    )
    update = next(t for t in tools if t.definition.name == "update_plan")

    registry = ToolRegistry()
    registry.register(update)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )
    call = ToolCall(
        name="update_plan",
        arguments={"step": 1, "status": "finished"},
    )
    outcome = executor.execute(call)

    assert outcome.malformed
    assert outcome.result is None
    assert "status" in outcome.model_text
    for status in STEP_STATUSES:
        assert status in outcome.model_text, f"expected '{status}' in error message"
    assert "update_plan(" in outcome.model_text


def test_update_plan_valid_status_passes(tmp_path: Path) -> None:
    """Regression: update_plan with a valid status is not rejected at validation."""
    from shellpilot.runtime.planner import PlanManager, make_plan_tools
    from shellpilot.tools.base import validate_args

    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=lambda plan, path: ("n", ""),
        get_user_intent=lambda: "intent",
    )
    update = next(t for t in tools if t.definition.name == "update_plan")
    error = validate_args(update, {"step": 1, "status": "completed"})
    assert error is None


# --- run_command.timeout_seconds minimum ---


def test_run_command_timeout_zero_is_malformed(tmp_path: Path) -> None:
    """run_command with timeout_seconds=0 is rejected via the malformed path."""
    from shellpilot.tools.command import RUN_COMMAND

    registry = ToolRegistry()
    registry.register(RUN_COMMAND)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )
    call = ToolCall(
        name="run_command",
        arguments={"argv": ["echo", "hi"], "timeout_seconds": 0},
    )
    outcome = executor.execute(call)

    assert outcome.malformed
    assert outcome.result is None
    assert "timeout_seconds" in outcome.model_text
    assert "1" in outcome.model_text  # minimum named
    assert "run_command(" in outcome.model_text


def test_run_command_timeout_positive_passes_validation(tmp_path: Path) -> None:
    """Regression: run_command with timeout_seconds=30 is not rejected at validation."""
    from shellpilot.tools.base import validate_args
    from shellpilot.tools.command import RUN_COMMAND

    error = validate_args(RUN_COMMAND, {"argv": ["echo", "hi"], "timeout_seconds": 30})
    assert error is None


# --- propose_plan.steps array item types ---


def test_propose_plan_non_string_step_is_malformed(tmp_path: Path) -> None:
    """propose_plan with steps=["ok", 42] is rejected via the malformed path."""
    from shellpilot.runtime.planner import PlanManager, make_plan_tools

    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=lambda plan, path: ("n", ""),
        get_user_intent=lambda: "intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")

    registry = ToolRegistry()
    registry.register(propose)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
    )
    call = ToolCall(
        name="propose_plan",
        arguments={"goal": "do thing", "steps": ["ok", 42]},
    )
    outcome = executor.execute(call)

    assert outcome.malformed
    assert outcome.result is None
    assert "steps" in outcome.model_text
    assert "string" in outcome.model_text
    assert "propose_plan(" in outcome.model_text


def test_propose_plan_valid_steps_passes_validation(tmp_path: Path) -> None:
    """Regression: propose_plan with valid string steps is not rejected at validation."""
    from shellpilot.runtime.planner import PlanManager, make_plan_tools
    from shellpilot.tools.base import validate_args

    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=lambda plan, path: ("n", ""),
        get_user_intent=lambda: "intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    error = validate_args(propose, {"goal": "do thing", "steps": ["step 1", "step 2"]})
    assert error is None


# --- malformed counter / schema-reminder integration (validate_args → runtime) ---


def test_enum_violation_increments_malformed_counter_and_sends_schema_reminder(
    tmp_path: Path,
) -> None:
    """An enum-validation failure uses the same malformed-call path as an unknown
    argument: the ConversationRuntime receives a schema-reminder message and the
    consecutive_malformed counter fires (two consecutive → no tools offered)."""
    from shellpilot.tools.patch import PATCH_FILE, WRITE_FILE
    from shellpilot.tools.registry import ToolRegistry as TR

    reg = TR()
    reg.register(PATCH_FILE)
    reg.register(WRITE_FILE)

    # Two consecutive malformed calls → tools stripped on the final call.
    fake = FakeLLM(
        script=[
            tool_call("patch_file", path="f.txt", operation="bad_op", old="x"),
            tool_call("write_file", path="f.txt", content="hi", mode="bad_mode"),
            answer("Stopped."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)
    # Inject the spec registrations into the runtime's registry.
    for spec in [PATCH_FILE, WRITE_FILE]:
        try:
            runtime.registry.register(spec)
        except Exception:
            pass  # already registered

    reply = runtime.run_turn("do something")
    assert reply == "Stopped."
    # After two consecutive malformed calls, the final model call must have no tools.
    assert fake.calls[-1].tools == ()
    # Schema reminder must have been sent.
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("patch_file(" in m.content or "write_file(" in m.content for m in tool_messages)


# ---------------------------------------------------------------------------
# Group E (E4): web_egress audit for NETWORK-side-effect tools
# ---------------------------------------------------------------------------


def test_network_tool_writes_web_egress_audit(tmp_path: Path) -> None:
    """A NETWORK-side-effect tool that runs writes a web_egress audit event
    recording the tool and its (redacted) args, but no off-box fallback."""
    import json

    from shellpilot.persistence.audit_store import AuditLogger

    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-web",
        workspace=tmp_path,
        profile="balanced",
    )

    net_spec = ToolSpec(
        definition=ToolDefinition(
            name="web_search",
            description="net tool",
            parameters={"query": {"type": "string"}},
            required=("query",),
        ),
        side_effect=SideEffect.NETWORK,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=lambda ctx, args: ToolResult(success=True, summary="ok", content="results"),
    )
    registry = ToolRegistry()
    registry.register(net_spec)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="balanced",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=lambda req: APPROVE,  # approve so the tool runs
        audit=audit,
    )

    executor.execute(ToolCall(name="web_search", arguments={"query": "python release"}))

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    egress = [e for e in events if e["event"] == "web_egress"]
    assert len(egress) == 1
    assert egress[0]["tool"] == "web_search"
    assert "python release" in json.dumps(egress[0])


def test_no_web_egress_for_non_network_tool(tmp_path: Path) -> None:
    """A NONE-side-effect tool writes no web_egress event."""
    import json

    from shellpilot.persistence.audit_store import AuditLogger

    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-local-tool",
        workspace=tmp_path,
        profile="balanced",
    )
    spec = _make_spec()
    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="balanced",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        audit=audit,
    )

    executor.execute(ToolCall(name="dummy", arguments={"x": "hello"}))

    text = (tmp_path / "audit.jsonl").read_text() if (tmp_path / "audit.jsonl").is_file() else ""
    events = [json.loads(line) for line in text.splitlines()] if text else []
    assert not any(e["event"] == "web_egress" for e in events)


def test_no_web_egress_when_network_tool_declined(tmp_path: Path) -> None:
    """A declined NETWORK tool never ran → no web_egress event (egress recorded
    only when the call actually leaves the box)."""
    import json

    from shellpilot.persistence.audit_store import AuditLogger

    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-declined",
        workspace=tmp_path,
        profile="balanced",
    )
    net_spec = ToolSpec(
        definition=ToolDefinition(
            name="web_fetch",
            description="net tool",
            parameters={"url": {"type": "string"}},
            required=("url",),
        ),
        side_effect=SideEffect.NETWORK,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=lambda ctx, args: ToolResult(success=True, summary="ok", content="page"),
    )
    registry = ToolRegistry()
    registry.register(net_spec)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="balanced",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=lambda req: DECLINE,  # decline
        audit=audit,
    )

    executor.execute(ToolCall(name="web_fetch", arguments={"url": "https://example.com"}))

    text = (tmp_path / "audit.jsonl").read_text() if (tmp_path / "audit.jsonl").is_file() else ""
    events = [json.loads(line) for line in text.splitlines()] if text else []
    assert not any(e["event"] == "web_egress" for e in events)


# ---------------------------------------------------------------------------
# Reject-and-steer (design section 14): the [e]dit approval outcome rejects the
# proposed action (NEVER runs it) and feeds the user's guidance back to the
# model so it re-proposes a corrected call through the normal gate.
# ---------------------------------------------------------------------------


def _side_effect_spec(name: str = "writer") -> tuple[ToolSpec, list[bool]]:
    """A side-effecting spec plus a list that records whether its handler ran."""
    ran: list[bool] = []

    def _handler(ctx: Any, args: Any) -> ToolResult:
        ran.append(True)
        return ToolResult(success=True, summary="wrote", content="")

    spec = ToolSpec(
        definition=ToolDefinition(
            name=name,
            description="side-effecting tool",
            parameters={"x": {"type": "string"}},
            required=("x",),
        ),
        side_effect=SideEffect.WORKSPACE_WRITE,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=_handler,
    )
    return spec, ran


def test_steer_does_not_run_the_action(tmp_path: Path) -> None:
    """[e]dit/STEER rejects the proposed action: the handler NEVER runs."""
    spec, ran = _side_effect_spec()
    executor = _make_executor(
        spec,
        tmp_path,
        ask_approval=lambda req: ApprovalReply(approved=False, steer_text="do X instead"),
    )

    outcome = executor.execute(ToolCall(name="writer", arguments={"x": "v"}))

    assert ran == []  # handler never invoked
    assert outcome.result is not None
    assert not outcome.result.success


def test_steer_guidance_reaches_the_model(tmp_path: Path) -> None:
    """The user's guidance text is carried in the model-facing outcome."""
    spec, _ = _side_effect_spec()
    executor = _make_executor(
        spec,
        tmp_path,
        ask_approval=lambda req: ApprovalReply(
            approved=False, steer_text="the dir is 'build' not 'bulid', use git clean"
        ),
    )

    outcome = executor.execute(ToolCall(name="writer", arguments={"x": "v"}))

    assert "the dir is 'build' not 'bulid', use git clean" in outcome.model_text
    # The model is told to propose a corrected action (not "do not retry").
    assert "Do not retry" not in outcome.model_text


def test_plain_decline_unchanged_with_new_reply_type(tmp_path: Path) -> None:
    """A plain decline (no steer text) keeps the existing do-not-retry feedback."""
    spec, ran = _side_effect_spec()
    executor = _make_executor(spec, tmp_path, ask_approval=lambda req: DECLINE)

    outcome = executor.execute(ToolCall(name="writer", arguments={"x": "v"}))

    assert ran == []
    assert "declined" in outcome.model_text
    assert "Do not retry" in outcome.model_text


def test_steer_audit_decision_is_steered(tmp_path: Path) -> None:
    """A steered approval is audited with decision=steered."""
    import json

    from shellpilot.persistence.audit_store import AuditLogger

    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-steer",
        workspace=tmp_path,
        profile="supervised",
    )
    spec, _ = _side_effect_spec()
    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=lambda req: ApprovalReply(approved=False, steer_text="do X instead"),
        audit=audit,
    )

    executor.execute(ToolCall(name="writer", arguments={"x": "v"}))

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    approvals = [e for e in events if e["event"] == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "steered"


# ---------------------------------------------------------------------------
# Display integrity: the approval panel shows the RESOLVED action path, never
# the raw (potentially spoofing) model argument (design sections 14.5, 36).
# ---------------------------------------------------------------------------


def _capture_request(spec: ToolSpec, tmp_path: Path, call: ToolCall) -> ApprovalRequest:
    """Run a call through the executor and return the ApprovalRequest it built."""
    captured: list[ApprovalRequest] = []

    def _ask(request: ApprovalRequest) -> ApprovalReply:
        captured.append(request)
        return DECLINE  # decline; we only want the request

    registry = ToolRegistry()
    registry.register(spec)
    executor = ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",  # ask before every side-effecting tool
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=_ask,
        snapshots=SnapshotStore(),
    )
    executor.execute(call)
    assert captured, "expected an approval request"
    return captured[0]


def test_approval_display_shows_resolved_path_not_spoof(tmp_path: Path) -> None:
    """A spoofing path argument displays as its resolved, workspace-relative
    target in the approval head, and matches the file actually acted on."""
    from shellpilot.tools.base import resolve_in_workspace
    from shellpilot.tools.patch import WRITE_FILE

    spoof = "notes/../secret.txt"
    request = _capture_request(
        WRITE_FILE,
        tmp_path,
        ToolCall(name="write_file", arguments={"path": spoof, "content": "x", "mode": "create"}),
    )

    # The raw, misleading argument must NOT appear in the approval display.
    assert spoof not in request.display
    # The resolved, workspace-relative target IS shown.
    assert "secret.txt" in request.display
    assert "notes/" not in request.display
    # Display == action: it names the same file resolve_in_workspace targets.
    acted_on = resolve_in_workspace(tmp_path, spoof)
    assert acted_on.name in request.display


def test_approval_display_marks_path_escaping_workspace(tmp_path: Path) -> None:
    """A path that resolves outside the workspace renders an honest marker in
    the display rather than a fabricated-looking path."""
    from shellpilot.tools.base import OUTSIDE_WORKSPACE_DISPLAY
    from shellpilot.tools.patch import WRITE_FILE

    escape = "../outside.txt"
    request = _capture_request(
        WRITE_FILE,
        tmp_path,
        ToolCall(name="write_file", arguments={"path": escape, "content": "x", "mode": "create"}),
    )
    assert escape not in request.display
    assert OUTSIDE_WORKSPACE_DISPLAY in request.display
