"""Tests for the plan manager and PLAN.md artifacts (design section 11)."""

from pathlib import Path

from shellpilot.runtime.planner import PlanManager, compact_plan_state


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
