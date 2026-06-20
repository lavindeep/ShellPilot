"""End-to-end plan and approval flows with the fake model."""

import json
from collections.abc import Sequence
from pathlib import Path

from shellpilot.config.model import RuntimeSettings, Settings
from shellpilot.llm.messages import Message
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_runtime(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    settings: Settings | None = None,
    audit: AuditLogger | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        audit=audit,
    )


def plan_call() -> Message:
    return tool_call(
        "propose_plan",
        goal="Add a feature",
        steps=["Inspect code", "Make change", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


def test_plan_proposal_approved_and_artifact_written(tmp_path: Path) -> None:
    # After approval, step 1 is active; the model completes the steps in-turn.
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            answer("All steps done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please add the feature")

    assert reply == "All steps done."
    assert ui.plan_approvals  # the user was shown the plan
    plan = runtime.plan_manager.active
    assert plan is not None
    artifact = runtime.plan_manager.artifact_path(plan)
    assert artifact.exists()
    assert "Add a feature" in artifact.read_text()
    # The model was told the plan is approved.
    approval_messages = [m for call in fake.calls for m in call.messages if m.role == "tool"]
    assert any("Plan approved" in m.content for m in approval_messages)


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


def test_e_then_repropose_single_task_dir_integration(tmp_path: Path) -> None:
    """Integration: e + re-propose must produce exactly one task directory."""
    revised_plan = tool_call(
        "propose_plan",
        goal="Add a feature",
        steps=["Inspect code", "Make change", "Verify rollback", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )
    fake = FakeLLM(
        script=[
            plan_call(),
            revised_plan,
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            tool_call("update_plan", step=4, status="completed"),
            answer("All done."),
        ]
    )
    # First approval returns "e", second returns "y"
    approval_answers: list[tuple[str, str]] = [("e", "add a rollback step"), ("y", "")]
    call_count = [0]

    class SequencedUI(FakeUI):
        def ask_plan_approval(self, plan: object, path: str) -> tuple[str, str]:
            idx = min(call_count[0], len(approval_answers) - 1)
            call_count[0] += 1
            answer_val = approval_answers[idx]
            self.plan_approvals.append((getattr(plan, "task_id", ""), path))
            return answer_val

    ui = SequencedUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please add the feature")

    assert reply == "All done."

    # Exactly ONE task directory on disk
    tasks_dir = tmp_path / ".shellpilot" / "tasks"
    task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
    assert len(task_dirs) == 1, f"expected 1 task dir, found {[d.name for d in task_dirs]}"

    # Revised step is in the PLAN.md
    plan_text = (task_dirs[0] / "PLAN.md").read_text()
    assert "Verify rollback" in plan_text

    # Progress log has a revision entry
    plan = runtime.plan_manager.active
    assert plan is not None
    assert any("revised: add a rollback step" in entry for entry in plan.progress_log)


def test_clear_with_pending_revision_next_propose_is_fresh(tmp_path: Path) -> None:
    """After /clear on a pending-revision state, next propose_plan creates a new task."""
    # First turn: propose, user picks "e"
    fake1 = FakeLLM(script=[plan_call(), answer("Let me revise.")])
    ui1 = FakeUI(plan_answer=("e", "make it shorter"))
    runtime = make_runtime(fake1, ui1, tmp_path)
    runtime.run_turn("Please add the feature")

    pm = runtime.plan_manager
    assert pm.pending_revision == "make it shorter"

    # Simulate /clear
    pm.cancel()
    assert pm.active is None
    assert pm.pending_revision is None

    # Second turn: propose fresh — no pending revision
    fresh_plan = tool_call(
        "propose_plan",
        goal="Completely different task",
        steps=["Step X", "Step Y", "Step Z"],
    )
    fake2 = FakeLLM(
        script=[
            fresh_plan,
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            answer("New task done."),
        ]
    )
    ui2 = FakeUI(plan_answer=("y", ""))
    runtime2 = make_runtime(fake2, ui2, tmp_path)
    # Re-use same plan_manager so state is shared
    runtime2.plan_manager = pm

    reply = runtime2.run_turn("Do something else")
    assert reply == "New task done."

    # There are now 2 task directories on disk (original + new one)
    tasks_dir = tmp_path / ".shellpilot" / "tasks"
    task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
    assert len(task_dirs) == 2


def test_update_plan_completes_steps(tmp_path: Path) -> None:
    # The model completes step 1 then stops calling tools while step 2 is still
    # active; the bounded nudge fires twice, then the turn ends on plain text.
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", step=1, status="completed", note="inspected"),
            answer("Step 1 done."),
            answer("Still narrating step 2."),
            answer("Stopping for now."),
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


# ---------------------------------------------------------------------------
# Bounded continuation nudge in the tool loop (task A3)
# ---------------------------------------------------------------------------


def two_step_plan_call() -> Message:
    return tool_call(
        "propose_plan",
        goal="Do the two-step thing",
        steps=["Read the source file", "Summarize it"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


def _nudge_messages(history: Sequence[Message]) -> list[Message]:
    return [
        m
        for m in history
        if getattr(m, "role", "") == "tool"
        and "The approved plan is not finished" in getattr(m, "content", "")
    ]


def test_text_reply_with_pending_plan_gets_nudged_and_continues(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("hello world")
    fake = FakeLLM(
        script=[
            two_step_plan_call(),
            # Plain narration with step 1 still active -> nudged.
            answer("I will now execute Step 1."),
            # The model resumes tool use in the SAME turn after the nudge.
            tool_call("read_file", path="source.txt"),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            answer("Both steps are done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please do the two-step thing")

    # The post-nudge tool call executed within this single run_turn.
    assert reply == "Both steps are done."
    assert any(name == "read_file" for name, _ in ui.tool_calls)
    nudges = _nudge_messages(runtime._history)
    assert len(nudges) == 1
    assert "next step 1" in getattr(nudges[0], "content", "")


def test_nudge_is_bounded_to_two(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            two_step_plan_call(),
            answer("I will now execute Step 1."),
            answer("Still thinking about Step 1."),
            answer("Final answer without tools."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please do the two-step thing")

    assert reply == "Final answer without tools."
    assert len(_nudge_messages(runtime._history)) == 2


def test_no_nudge_while_plan_only_proposed(tmp_path: Path) -> None:
    # Edit choice leaves the plan at status "proposed" (awaiting a revised
    # proposal). A no-tool-call reply in that state must end the turn, not nudge.
    fake = FakeLLM(script=[two_step_plan_call(), answer("Let me reconsider the plan.")])
    ui = FakeUI(plan_answer=("e", "make it three steps"))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please do the two-step thing")

    assert reply == "Let me reconsider the plan."
    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.status == "proposed"
    assert _nudge_messages(runtime._history) == []


def test_no_nudge_without_active_plan(tmp_path: Path) -> None:
    fake = FakeLLM(script=[answer("Just a plain answer.")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("hello")

    assert reply == "Just a plain answer."
    assert _nudge_messages(runtime._history) == []
    assert len(fake.calls) == 1


def test_no_nudge_when_plan_completed(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            two_step_plan_call(),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            answer("All done, summary follows."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("Please do the two-step thing")

    assert reply == "All done, summary follows."
    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.status == "completed"
    assert _nudge_messages(runtime._history) == []


def test_no_nudge_after_tool_budget_exhausted(tmp_path: Path) -> None:
    settings = Settings(runtime=RuntimeSettings(max_tool_turns=1))
    fake = FakeLLM(
        script=[
            two_step_plan_call(),
            # Second tool call trips the budget (max_tool_turns=1), which empties
            # tools and tells the model to answer in text. The plan is still
            # pending, but with tools emptied the nudge must NOT fire.
            tool_call("update_plan", note="thinking"),
            answer("Wrapping up in plain text."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path, settings)

    reply = runtime.run_turn("Please do the two-step thing")

    assert reply == "Wrapping up in plain text."
    assert any("budget" in s.lower() for s in ui.statuses)
    assert _nudge_messages(runtime._history) == []


def test_nudge_audited(tmp_path: Path) -> None:
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess1",
        workspace=tmp_path,
        profile="balanced",
    )
    (tmp_path / "source.txt").write_text("hello world")
    fake = FakeLLM(
        script=[
            two_step_plan_call(),
            answer("I will now execute Step 1."),
            tool_call("read_file", path="source.txt"),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            answer("Both steps are done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path, audit=audit)

    runtime.run_turn("Please do the two-step thing")

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    nudge_events = [e for e in events if e["event"] == "plan_nudge"]
    assert len(nudge_events) == 1
    assert nudge_events[0]["summary"] == "step 1"


# ---------------------------------------------------------------------------
# Plan-step completion guard (Fix B): a step whose last side-effecting action
# failed cannot be marked completed until something succeeds or a blocker is set.
# ---------------------------------------------------------------------------


def _edit_plan_call() -> Message:
    return tool_call(
        "propose_plan",
        goal="Apply the change",
        steps=["Patch the file", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


def test_complete_after_rejected_patch_is_refused_end_to_end(tmp_path: Path) -> None:
    # Headline regression: an approved plan whose patch is rejected (anchor not
    # found) must NOT advance when the model calls update_plan(completed).
    target = tmp_path / "module.py"
    target.write_text("def real_function():\n    return 1\n")
    fake = FakeLLM(
        script=[
            _edit_plan_call(),
            # Read records the snapshot so the patch reaches anchor matching.
            tool_call("read_file", path="module.py"),
            # Anchor that does NOT appear in the file -> "edit rejected".
            tool_call(
                "patch_file",
                path="module.py",
                operation="replace_exact",
                old="def nonexistent_anchor():",
                new="def patched():",
            ),
            # The model wrongly marks the step done; the guard refuses.
            tool_call("update_plan", step=1, status="completed"),
            # Step 1 is still active, so a plain reply gets nudged (bounded to 2);
            # absorb both nudges so the turn ends cleanly.
            answer("I could not apply the edit."),
            answer("Still could not apply the edit."),
            answer("Stopping; the edit cannot be applied."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""), approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Apply the change")

    # patch_file was rejected.
    patch_results = [r for r in ui.tool_results if r[0] == "patch_file"]
    assert patch_results and patch_results[0][1] is False
    assert patch_results[0][2] == "edit rejected"

    # The update_plan completion was refused.
    update_results = [r for r in ui.tool_results if r[0] == "update_plan"]
    assert update_results and update_results[-1][1] is False
    assert "not completed" in update_results[-1][2]

    # The recorded tool result carries the corrective failure and guidance.
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("not completed" in m.content for m in tool_messages)
    assert any("update_plan(blocker=" in m.content for m in tool_messages)

    # Step 1 still active; plan still active; step 2 not advanced.
    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.status == "active"
    assert plan.steps[0].status == "active"
    assert plan.steps[1].status == "pending"


def test_rejected_patch_then_write_completes_and_advances(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            _edit_plan_call(),
            # Patch a file that does not exist yet -> rejected.
            tool_call(
                "patch_file",
                path="new.py",
                operation="replace_exact",
                old="anything",
                new="x",
            ),
            # Successful write_file for the same step unblocks completion.
            tool_call("write_file", path="new.py", content="print('hi')\n", mode="create"),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            answer("Done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""), approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Apply the change")

    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.steps[0].status == "completed"
    assert plan.steps[1].status == "completed"
    assert plan.status == "completed"


def test_read_only_step_completes_end_to_end(tmp_path: Path) -> None:
    # False-positive guard: a read_file-only step has no failed side effect and
    # must complete freely.
    (tmp_path / "module.py").write_text("hello\n")
    fake = FakeLLM(
        script=[
            tool_call(
                "propose_plan",
                goal="Inspect then test",
                steps=["Read the module", "Run tests"],
                assumptions=["repo is clean"],
                verification=["pytest"],
            ),
            tool_call("read_file", path="module.py"),
            tool_call("update_plan", step=1, status="completed"),
            # Step 2 is now active, so a plain reply is nudged (bounded to 2).
            answer("Inspected."),
            answer("Inspected; nothing more to do here."),
            answer("Done inspecting."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Inspect the module")

    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.steps[0].status == "completed"
    assert plan.steps[1].status == "active"


def test_failing_run_command_on_active_step_blocks_completion(tmp_path: Path) -> None:
    # A nonzero-exit run_command is success=False and must block completion.
    fake = FakeLLM(
        script=[
            _edit_plan_call(),
            tool_call("run_command", argv=["false"]),
            tool_call("update_plan", step=1, status="completed"),
            # Step 1 still active after the refusal -> plain reply nudged (x2).
            answer("The command failed."),
            answer("The command still failed."),
            answer("Stopping; the command keeps failing."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""), approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Apply the change")

    update_results = [r for r in ui.tool_results if r[0] == "update_plan"]
    assert update_results and update_results[-1][1] is False
    plan = runtime.plan_manager.active
    assert plan is not None
    assert plan.steps[0].status == "active"
    assert plan.steps[1].status == "pending"
