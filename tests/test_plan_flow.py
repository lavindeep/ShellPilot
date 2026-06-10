"""End-to-end plan and approval flows with the fake model."""

from pathlib import Path

from shellpilot.config.model import RuntimeSettings, Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


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


def plan_call() -> object:
    return tool_call(
        "propose_plan",
        goal="Add a feature",
        steps=["Inspect code", "Make change", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


def test_plan_proposal_approved_and_artifact_written(tmp_path: Path) -> None:
    fake = FakeLLM(script=[plan_call(), answer("Starting with step 1.")])
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please add the feature")

    assert reply == "Starting with step 1."
    assert ui.plan_approvals  # the user was shown the plan
    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.status == "active"
    artifact = runtime.plan_manager.artifact_path(plan)
    assert artifact.exists()
    assert "Add a feature" in artifact.read_text()
    # The model was told the plan is approved.
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("Plan approved" in m.content for m in tool_messages)


def test_plan_rejection_stops_execution(tmp_path: Path) -> None:
    fake = FakeLLM(script=[plan_call(), answer("Okay, what would you like instead?")])
    ui = FakeUI(plan_answer=("n", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Please add the feature")

    assert runtime.plan_manager.active is None
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("rejected" in m.content for m in tool_messages)


def test_plan_edit_requests_revision(tmp_path: Path) -> None:
    fake = FakeLLM(script=[plan_call(), answer("Here is a revised approach.")])
    ui = FakeUI(plan_answer=("e", "skip the tests step"))
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Please add the feature")

    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("skip the tests step" in m.content for m in tool_messages)


def test_update_plan_completes_steps(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", step=1, status="completed", note="inspected"),
            answer("Step 1 done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.run_turn("Please add the feature")

    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.steps[0].status == "completed"
    assert plan.steps[1].status == "active"


def test_blocker_tool_blocks_plan_and_instructs_protocol(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", blocker="pytest fails: ModuleNotFoundError"),
            answer("I hit a roadblock and recorded it."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.run_turn("Please add the feature")

    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.status == "blocked"
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("roadblock" in m.content.lower() for m in tool_messages)


def test_medium_command_asks_approval_and_runs_when_approved(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("run_command", argv=["touch", "new.txt"]),
            answer("Created the file."),
        ]
    )
    ui = FakeUI(approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("create new.txt")

    assert len(ui.approval_requests) == 1
    request = ui.approval_requests[0]
    assert request.risk is RiskLevel.MEDIUM
    assert request.display == "touch new.txt"
    assert (tmp_path / "new.txt").exists()


def test_declined_command_does_not_run(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("run_command", argv=["touch", "new.txt"]),
            answer("Understood, not creating it."),
        ]
    )
    ui = FakeUI(approve_actions=False)
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("create new.txt")

    assert not (tmp_path / "new.txt").exists()
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("declined" in m.content for m in tool_messages)


def test_low_risk_command_runs_without_approval_in_balanced(tmp_path: Path) -> None:
    fake = FakeLLM(script=[tool_call("run_command", argv=["pwd"]), answer("done")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("where are we")

    assert ui.approval_requests == []  # auto-approved low risk


def test_supervised_profile_asks_even_for_low_risk_commands(tmp_path: Path) -> None:
    settings = Settings(runtime=RuntimeSettings(security_profile="supervised"))
    fake = FakeLLM(script=[tool_call("run_command", argv=["pwd"]), answer("done")])
    ui = FakeUI(approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path, settings)

    runtime.run_turn("where are we")

    assert len(ui.approval_requests) == 1


def test_repeated_identical_failure_triggers_roadblock_guidance(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="missing.txt"),
            tool_call("read_file", path="missing.txt"),
            answer("I recorded the roadblock."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("read missing.txt")

    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("failed twice" in m.content for m in tool_messages)
