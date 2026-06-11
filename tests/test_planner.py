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
