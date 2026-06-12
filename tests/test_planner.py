"""Tests for the plan manager and PLAN.md artifacts (design section 11)."""

from pathlib import Path
from typing import Any

from shellpilot.runtime.planner import PlanManager, TaskPlan, compact_plan_state, make_plan_tools
from shellpilot.tools.base import ToolContext


def make_plan(tmp_path: Path) -> PlanManager:
    manager = PlanManager(tmp_path, "balanced")
    manager.create(
        goal="Refactor the command runner",
        user_intent="User wants a cleaner execution layer",
        steps=["Inspect current code", "Define interface", "Run tests"],
        assumptions=["Keep local-only behavior"],
        verification=["pytest"],
    )
    return manager


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, max_result_tokens=2000)


def _approval_asker(choice: str) -> Any:
    def ask(plan: TaskPlan, path: str) -> tuple[str, str]:
        return (choice, "")

    return ask


def test_create_writes_artifact_with_template_sections(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    assert manager.active is not None
    path = manager.artifact_path(manager.active)
    assert path.exists()
    assert ".shellpilot/tasks/" in str(path)
    text = path.read_text()
    for section in (
        "# Task Plan: Refactor the command runner",
        "## Goal",
        "## User Intent",
        "## Assumptions",
        "## Plan",
        "## Verification",
        "## Decisions",
        "## Open Questions",
        "## Blockers",
        "## Revisions",
        "## Progress Log",
    ):
        assert section in text, f"missing {section}"
    assert "Status: proposed" in text


def test_approve_activates_first_step(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    manager.approve()
    assert manager.active is not None
    assert manager.active.status == "active"
    assert manager.active.steps[0].status == "active"


def test_step_completion_advances_and_finishes(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    manager.approve()
    assert manager.update_step(1, "completed") == ""
    assert manager.active is not None
    assert manager.active.steps[1].status == "active"
    manager.update_step(2, "completed")
    manager.update_step(3, "completed")
    assert manager.active.status == "completed"
    text = manager.artifact_path(manager.active).read_text()
    assert "- [x] Inspect current code" in text


def test_invalid_step_index_reports(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    error = manager.update_step(9, "completed")
    assert "no step 9" in error


def test_blocker_blocks_plan_and_is_recorded(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    manager.approve()
    manager.record_blocker("pytest fails: ModuleNotFoundError")
    assert manager.active is not None
    assert manager.active.status == "blocked"
    text = manager.artifact_path(manager.active).read_text()
    assert "ModuleNotFoundError" in text


def test_cancel_clears_active(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    assert manager.active is not None
    path = manager.artifact_path(manager.active)
    manager.cancel()
    assert manager.active is None
    assert "Status: cancelled" in path.read_text()


def test_compact_plan_state_is_small(tmp_path: Path) -> None:
    manager = make_plan(tmp_path)
    manager.approve()
    assert manager.active is not None
    block = compact_plan_state(manager.active)
    assert "Refactor the command runner" in block
    assert len(block) < 600


# ---------------------------------------------------------------------------
# Tool-result continuation instruction tests (design section 11, task A2)
# ---------------------------------------------------------------------------


def test_approval_result_instructs_same_turn_execution(tmp_path: Path) -> None:
    """Approved plan ToolResult must tell the model to continue in this same turn."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)
    result = propose.handler(
        ctx,
        {
            "goal": "Build the feature",
            "steps": ["Inspect code", "Make change", "Run tests"],
        },
    )
    assert result.success
    assert "this same turn" in result.content
    assert "step 1" in result.content.lower()
    assert "never ask" in result.content


def test_update_result_pushes_next_step(tmp_path: Path) -> None:
    """Non-final update_plan result must end with the continuation sentence.
    Final update_plan result must NOT contain it."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)

    # Approve the plan first
    propose.handler(
        ctx,
        {
            "goal": "Build the feature",
            "steps": ["Inspect code", "Make change", "Run tests"],
        },
    )
    assert manager.active is not None

    # Non-final step: completing step 1 (still steps 2, 3 remaining)
    result = update.handler(ctx, {"step": 1, "status": "completed"})
    assert result.success
    assert result.content.endswith("Continue with the next step now, in this same turn.")

    # Final step: completing step 3 (all done)
    update.handler(ctx, {"step": 2, "status": "completed"})
    final_result = update.handler(ctx, {"step": 3, "status": "completed"})
    assert final_result.success
    assert "Continue with the next step now, in this same turn." not in final_result.content
    assert "All steps complete" in final_result.content


def test_blocker_update_does_not_push_continuation(tmp_path: Path) -> None:
    """A blocker update returns the roadblock guidance and must NOT end with the
    same-turn continuation sentence (the blocker branch returns early)."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)

    propose.handler(
        ctx,
        {
            "goal": "Build the feature",
            "steps": ["Inspect code", "Make change", "Run tests"],
        },
    )
    assert manager.active is not None

    result = update.handler(ctx, {"blocker": "pytest fails: ModuleNotFoundError"})
    assert result.success
    assert "Continue with the next step now, in this same turn." not in result.content
    assert "roadblock" in result.content.lower()


# ---------------------------------------------------------------------------
# Plan revision (fix: "e" + re-propose must update the same task, not create new)
# ---------------------------------------------------------------------------


def _approval_asker_sequence(answers: list[tuple[str, str]]) -> Any:
    """Returns answers in sequence; last answer is repeated if exhausted."""
    idx = [0]

    def ask(plan: TaskPlan, path: str) -> tuple[str, str]:
        result = answers[min(idx[0], len(answers) - 1)]
        idx[0] += 1
        return result

    return ask


def test_e_then_repropose_updates_same_task(tmp_path: Path) -> None:
    """e + feedback then a new propose_plan must reuse the same task_id and directory."""
    feedback = "add a rollback step"
    manager = PlanManager(tmp_path, "balanced")
    # First approval returns "e", second returns "y"
    asker = _approval_asker_sequence([("e", feedback), ("y", "")])
    tools = make_plan_tools(
        manager,
        ask_plan_approval=asker,
        get_user_intent=lambda: "user intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    # First propose_plan call — user picks "e"
    result1 = propose.handler(
        ctx,
        {
            "goal": "Deploy the app",
            "steps": ["Build", "Push", "Start service"],
        },
    )
    assert result1.success
    assert manager.active is not None
    original_task_id = manager.active.task_id
    # The ToolResult must tell the model to stay in the same task
    assert original_task_id in result1.content
    assert feedback in result1.content

    # Second propose_plan call — model submits the revised plan
    result2 = propose.handler(
        ctx,
        {
            "goal": "Deploy the app",
            "steps": ["Build", "Push", "Rollback if needed", "Start service"],
        },
    )
    assert result2.success
    assert manager.active is not None
    # Same task_id — no new task created
    assert manager.active.task_id == original_task_id

    # Exactly one task directory on disk
    tasks_dir = tmp_path / ".shellpilot" / "tasks"
    task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
    assert len(task_dirs) == 1, f"expected 1 task dir, found {[d.name for d in task_dirs]}"
    assert task_dirs[0].name == original_task_id

    # PLAN.md reflects the revised steps
    plan_text = (tasks_dir / original_task_id / "PLAN.md").read_text()
    assert "Rollback if needed" in plan_text

    # progress_log records the revision entry
    assert any(f"revised: {feedback}" in entry for entry in manager.active.progress_log)

    # Pending revision marker is cleared
    assert manager.pending_revision is None


def test_approve_after_revision_proceeds_normally(tmp_path: Path) -> None:
    """After a revision cycle the approved plan can be executed."""
    feedback = "skip the test step"
    manager = PlanManager(tmp_path, "balanced")
    asker = _approval_asker_sequence([("e", feedback), ("y", "")])
    tools = make_plan_tools(
        manager,
        ask_plan_approval=asker,
        get_user_intent=lambda: "user intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    propose.handler(ctx, {"goal": "Do thing", "steps": ["Step A", "Step B"]})
    result = propose.handler(ctx, {"goal": "Do thing", "steps": ["Step A"]})

    assert result.success
    assert manager.active is not None
    assert manager.active.status == "active"
    # Can advance the step without errors
    err = manager.update_step(1, "completed")
    assert err == ""
    assert manager.active.status == "completed"


def test_plain_propose_without_pending_revision_creates_fresh_task(tmp_path: Path) -> None:
    """A plain propose_plan with no pending revision creates a new task (regression guard)."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("n"),
        get_user_intent=lambda: "user intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    # First propose — user rejects (no pending revision)
    propose.handler(ctx, {"goal": "Task one", "steps": ["A", "B", "C"]})

    # No active plan, no pending revision
    assert manager.active is None
    assert manager.pending_revision is None

    # Change approver to accept
    manager2 = PlanManager(tmp_path, "balanced")
    tools2 = make_plan_tools(
        manager2,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "user intent",
    )
    propose2 = next(t for t in tools2 if t.definition.name == "propose_plan")
    propose2.handler(ctx, {"goal": "Task two", "steps": ["X", "Y", "Z"]})

    assert manager2.active is not None
    assert "Task two" in manager2.active.goal


def test_cancel_with_pending_revision_clears_state(tmp_path: Path) -> None:
    """cancel() must clear pending_revision so the next propose creates a fresh task."""
    feedback = "add a step"
    manager = PlanManager(tmp_path, "balanced")
    asker = _approval_asker_sequence([("e", feedback)])
    tools = make_plan_tools(
        manager,
        ask_plan_approval=asker,
        get_user_intent=lambda: "user intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    propose.handler(ctx, {"goal": "Some task", "steps": ["Do X", "Do Y"]})

    # A pending revision is now set
    assert manager.pending_revision == feedback

    # Simulate /clear cancelling
    manager.cancel()

    assert manager.active is None
    assert manager.pending_revision is None

    # Next propose must create a fresh task
    original_task_dir_count = sum(
        1 for _ in (tmp_path / ".shellpilot" / "tasks").iterdir() if _.is_dir()
    )

    manager2 = PlanManager(tmp_path, "balanced")
    tools2 = make_plan_tools(
        manager2,
        ask_plan_approval=_approval_asker("n"),
        get_user_intent=lambda: "user intent",
    )
    propose2 = next(t for t in tools2 if t.definition.name == "propose_plan")
    propose2.handler(ctx, {"goal": "Fresh task", "steps": ["P", "Q", "R"]})

    # A new task directory was created (total went up by 1)
    new_count = sum(1 for _ in (tmp_path / ".shellpilot" / "tasks").iterdir() if _.is_dir())
    assert new_count == original_task_dir_count + 1


# ---------------------------------------------------------------------------
# Plan-step completion guard (Fix B): refuse completing a step whose last
# side-effecting action failed and nothing has succeeded since.
# ---------------------------------------------------------------------------


def test_complete_after_failed_side_effect_is_refused(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    manager.note_side_effect(False)
    result = update.handler(ctx, {"step": 1, "status": "completed"})

    assert result.success is False
    assert "not completed" in result.summary
    assert "edit rejected" in result.content
    assert "update_plan(blocker=" in result.content
    # Step 1 stays active; plan stays active; step 2 not advanced.
    assert manager.active.steps[0].status == "active"
    assert manager.active.status == "active"
    assert manager.active.steps[1].status == "pending"


def test_failure_then_success_completes_and_advances(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    manager.note_side_effect(False)
    manager.note_side_effect(True)
    result = update.handler(ctx, {"step": 1, "status": "completed"})

    assert result.success is True
    assert manager.active.steps[0].status == "completed"
    assert manager.active.steps[1].status == "active"


def test_pure_analysis_step_completes_freely(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Read code", "Run tests"]})
    assert manager.active is not None

    # No note_side_effect calls at all: a read-only step completes freely.
    result = update.handler(ctx, {"step": 1, "status": "completed"})

    assert result.success is True
    assert manager.active.steps[0].status == "completed"
    assert manager.active.steps[1].status == "active"


def test_retry_after_block_then_success_completes(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    manager.note_side_effect(False)
    refused = update.handler(ctx, {"step": 1, "status": "completed"})
    assert refused.success is False
    assert manager.active.steps[0].status == "active"

    # The model retries the edit and it succeeds, then completes.
    manager.note_side_effect(True)
    done = update.handler(ctx, {"step": 1, "status": "completed"})
    assert done.success is True
    assert manager.active.steps[0].status == "completed"
    assert manager.active.steps[1].status == "active"


def test_counters_reset_on_step_advance(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Adjust config"]})
    assert manager.active is not None

    # Step 1: fail then succeed then complete.
    manager.note_side_effect(False)
    manager.note_side_effect(True)
    update.handler(ctx, {"step": 1, "status": "completed"})
    assert manager.active.steps[1].status == "active"

    # Step 2: no attempts at all -> step-1 failure must not leak in.
    result = update.handler(ctx, {"step": 2, "status": "completed"})
    assert result.success is True
    assert manager.active.steps[1].status == "completed"
    assert manager.active.status == "completed"


def test_denial_counts_as_failure_and_blocks(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    # A denied approval is success=False -> note_side_effect(False).
    manager.note_side_effect(False)
    result = update.handler(ctx, {"step": 1, "status": "completed"})

    assert result.success is False
    assert manager.active.steps[0].status == "active"


def test_blocker_path_still_works_after_a_failure(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    manager.note_side_effect(False)
    result = update.handler(ctx, {"blocker": "anchor not found; edit rejected after approval"})

    assert result.success is True
    assert manager.active.status == "blocked"
    assert "roadblock" in result.content.lower()
    assert any("anchor not found" in b for b in manager.active.blockers)


def test_invalid_status_message_mentions_blocker_argument(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})

    result = update.handler(ctx, {"step": 1, "status": "blocker"})

    assert result.success is False
    assert "blocker argument" in result.content
    assert "update_plan(blocker=" in result.content


def test_completing_non_active_step_is_not_guarded(tmp_path: Path) -> None:
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    update = next(t for t in tools if t.definition.name == "update_plan")
    ctx = _ctx(tmp_path)
    propose.handler(ctx, {"goal": "Build", "steps": ["Edit code", "Run tests"]})
    assert manager.active is not None

    # Step 1 is the active step; a failure is on the active step. Completing a
    # *different* (non-active) step 2 must not be guarded by step 1's failure.
    manager.note_side_effect(False)
    result = update.handler(ctx, {"step": 2, "status": "completed"})

    assert result.success is True
    assert manager.active.steps[1].status == "completed"


# ---------------------------------------------------------------------------
# max_plan_steps enforcement (v0.5.2, design section 11)
# A proposal that exceeds the limit returns a CORRECTIVE FAILURE (normal failed
# ToolResult, not the malformed path) with the "consolidate" message.
# ---------------------------------------------------------------------------


def test_propose_plan_exceeding_max_steps_returns_corrective_failure(tmp_path: Path) -> None:
    """11 steps when max_plan_steps=10 returns a failed ToolResult with the
    'consolidate' message — it is NOT a malformed call."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
        max_plan_steps=10,
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    steps_11 = [f"Step {i}" for i in range(1, 12)]
    result = propose.handler(ctx, {"goal": "Big task", "steps": steps_11})

    assert result.success is False
    assert "11" in result.summary
    assert "10" in result.summary
    assert "consolidate" in result.content
    # No plan was created.
    assert manager.active is None


def test_propose_plan_at_max_steps_is_accepted(tmp_path: Path) -> None:
    """Exactly max_plan_steps steps is accepted (boundary is inclusive)."""
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
        max_plan_steps=10,
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    ctx = _ctx(tmp_path)

    steps_10 = [f"Step {i}" for i in range(1, 11)]
    result = propose.handler(ctx, {"goal": "Big task", "steps": steps_10})

    assert result.success is True
    assert manager.active is not None
    assert len(manager.active.steps) == 10


def test_propose_plan_custom_max_steps_honored(tmp_path: Path) -> None:
    """A custom max_plan_steps=3 rejects 4-step plans and accepts 3-step plans."""
    ctx = _ctx(tmp_path)

    # 4 steps with max=3 → rejected.
    manager = PlanManager(tmp_path, "balanced")
    tools = make_plan_tools(
        manager,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
        max_plan_steps=3,
    )
    propose = next(t for t in tools if t.definition.name == "propose_plan")
    result = propose.handler(ctx, {"goal": "task", "steps": ["A", "B", "C", "D"]})
    assert result.success is False
    assert "3" in result.summary
    assert "consolidate" in result.content

    # 3 steps with max=3 → accepted.
    manager2 = PlanManager(tmp_path, "balanced")
    tools2 = make_plan_tools(
        manager2,
        ask_plan_approval=_approval_asker("y"),
        get_user_intent=lambda: "test intent",
        max_plan_steps=3,
    )
    propose2 = next(t for t in tools2 if t.definition.name == "propose_plan")
    result2 = propose2.handler(ctx, {"goal": "task", "steps": ["A", "B", "C"]})
    assert result2.success is True
    assert manager2.active is not None
