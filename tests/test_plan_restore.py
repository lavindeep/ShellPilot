"""Tests for plan-restore-on-resume (design section 11.3)."""

from __future__ import annotations

import json
from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.llm.messages import Message
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.sessions import SessionStore
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_runtime(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    settings: Settings | None = None,
    session: SessionStore | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        session=session,
    )


def plan_call() -> Message:
    return tool_call(
        "propose_plan",
        goal="Add a feature",
        steps=["Inspect code", "Make change", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


# ---------------------------------------------------------------------------
# Test 1: pointer recorded at transition
# ---------------------------------------------------------------------------


def test_active_plan_pointer_recorded_at_approval(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", "test-sess")
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            answer("Done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path, session=store)
    runtime.run_turn("add a feature")

    raw_lines = [json.loads(line) for line in store.path.read_text().splitlines() if line.strip()]
    plan_records = [rec for rec in raw_lines if rec.get("type") == "active_plan"]
    assert any(r.get("task_id") is not None for r in plan_records)
    plan = runtime.plan_manager.active
    assert plan is not None
    assert any(r.get("task_id") == plan.task_id for r in plan_records)


# ---------------------------------------------------------------------------
# Test 2: restore appends NO new active_plan record
# ---------------------------------------------------------------------------


def test_restore_does_not_record_new_pointer(tmp_path: Path) -> None:
    from shellpilot.runtime.planner import PlanManager

    # Create an active plan using PlanManager directly (bypasses runtime/session)
    manager = PlanManager(tmp_path, "balanced")
    plan = manager.create(
        goal="Add a feature",
        user_intent="test",
        steps=["Step A", "Step B"],
        assumptions=[],
        verification=[],
    )
    manager.approve()
    task_id = plan.task_id

    # Write a session file that references this plan
    store = SessionStore(tmp_path / "sessions", "test-sess")
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_active_plan(task_id)
    bytes_before = store.path.read_bytes()

    # Restore in a new runtime — should not write anything to session file
    loaded = SessionStore.load(store.path)
    store2 = SessionStore(tmp_path / "sessions", "test-sess")
    runtime2 = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path, session=store2)
    runtime2.restore_history(loaded.messages)
    runtime2.restore_active_plan(loaded.active_plan_task_id)

    # File must be unchanged (restore primes the dedupe cache)
    assert store.path.read_bytes() == bytes_before


# ---------------------------------------------------------------------------
# Test 3: full round-trip
# ---------------------------------------------------------------------------


def test_full_round_trip_plan_restore(tmp_path: Path) -> None:
    """Drive plan to active-with-step-1-completed, then restore in a new runtime."""
    from shellpilot.runtime.planner import PlanManager

    # Create plan directly so we control its state precisely
    manager = PlanManager(tmp_path, "balanced")
    plan = manager.create(
        goal="Add a feature",
        user_intent="do the thing",
        steps=["Inspect code", "Make change", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )
    manager.approve()
    manager.update_step(1, "completed")
    original_task_id = plan.task_id
    assert plan.status == "active"
    assert plan.steps[0].status == "completed"

    # Write a session file referencing the active plan
    store = SessionStore(tmp_path / "sessions", "rt-sess")
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_active_plan(original_task_id)

    # Load and restore into a new runtime
    loaded = SessionStore.load(store.path)
    assert loaded.active_plan_task_id == original_task_id

    store2 = SessionStore(tmp_path / "sessions", "rt-sess")
    runtime2 = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path, session=store2)
    runtime2.restore_history(loaded.messages)
    runtime2.restore_active_plan(loaded.active_plan_task_id)

    restored = runtime2.plan_manager.active
    assert restored is not None
    assert restored.task_id == original_task_id
    assert restored.status == "active"
    assert len(restored.steps) == 3
    assert restored.steps[0].status == "completed"


# ---------------------------------------------------------------------------
# Test 4: TaskPlan sidecar round-trip
# ---------------------------------------------------------------------------


def test_taskplan_sidecar_roundtrip(tmp_path: Path) -> None:
    from shellpilot.runtime.planner import PlanManager, load_plan

    manager = PlanManager(tmp_path, "balanced")
    plan = manager.create(
        goal="Test goal",
        user_intent="test",
        steps=["Step A", "Step B"],
        assumptions=["assume x"],
        verification=["check y"],
    )
    # Sidecar must exist
    sidecar = manager.artifact_path(plan).parent / "state.json"
    assert sidecar.exists()

    loaded = load_plan(tmp_path, plan.task_id)
    assert loaded is not None
    assert loaded.task_id == plan.task_id
    assert loaded.goal == plan.goal
    assert len(loaded.steps) == 2
    assert loaded.steps[0].title == "Step A"


# ---------------------------------------------------------------------------
# Test 5: completed plan not restored
# ---------------------------------------------------------------------------


def test_completed_plan_not_restored(tmp_path: Path) -> None:
    from shellpilot.runtime.planner import PlanManager, load_plan

    manager = PlanManager(tmp_path, "balanced")
    plan = manager.create(
        goal="g", user_intent="u", steps=["A", "B"], assumptions=[], verification=[]
    )
    manager.approve()
    manager.update_step(1, "completed")
    manager.update_step(2, "completed")
    assert plan.status == "completed"

    # load_plan returns the plan (it's in the sidecar)
    loaded = load_plan(tmp_path, plan.task_id)
    assert loaded is not None
    assert loaded.status == "completed"

    # restore_active_plan should not restore completed plans
    store = SessionStore(tmp_path / "sessions", "sess")
    runtime = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path, session=store)
    runtime.restore_active_plan(plan.task_id)
    assert runtime.plan_manager.active is None


# ---------------------------------------------------------------------------
# Test 6: missing sidecar → no-op, no crash
# ---------------------------------------------------------------------------


def test_missing_sidecar_no_crash(tmp_path: Path) -> None:
    from shellpilot.runtime.planner import load_plan

    result = load_plan(tmp_path, "nonexistent-task-id")
    assert result is None

    store = SessionStore(tmp_path / "sessions", "sess")
    runtime = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path, session=store)
    runtime.restore_active_plan("nonexistent-task-id")  # should not raise
    assert runtime.plan_manager.active is None


# ---------------------------------------------------------------------------
# Test 7: corrupt sidecar → None
# ---------------------------------------------------------------------------


def test_corrupt_sidecar_returns_none(tmp_path: Path) -> None:
    from shellpilot.persistence.paths import project_state_dir
    from shellpilot.runtime.planner import load_plan

    task_id = "20260101-000000-test-task"
    sidecar_dir = project_state_dir(tmp_path) / "tasks" / task_id
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "state.json").write_text("not-valid-json{{{")

    result = load_plan(tmp_path, task_id)
    assert result is None


# ---------------------------------------------------------------------------
# Test 8: wrong state_version → None
# ---------------------------------------------------------------------------


def test_wrong_state_version_returns_none(tmp_path: Path) -> None:
    from shellpilot.persistence.paths import project_state_dir
    from shellpilot.runtime.planner import load_plan

    task_id = "20260101-000001-version-task"
    sidecar_dir = project_state_dir(tmp_path) / "tasks" / task_id
    sidecar_dir.mkdir(parents=True)
    data = {"state_version": 99, "task_id": task_id, "goal": "g"}
    (sidecar_dir / "state.json").write_text(json.dumps(data))

    result = load_plan(tmp_path, task_id)
    assert result is None


# ---------------------------------------------------------------------------
# Test 9: pre-0.6.0 transcript loads with active_plan_task_id=None
# ---------------------------------------------------------------------------


def test_pre_060_transcript_has_no_plan_pointer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", "old-sess")
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_message(Message(role="user", content="hello"))
    store.record_message(Message(role="assistant", content="hi"))
    # No active_plan records — old transcript
    loaded = SessionStore.load(store.path)
    assert loaded.active_plan_task_id is None


# ---------------------------------------------------------------------------
# Test 10: clear record drops pointer
# ---------------------------------------------------------------------------


def test_clear_record_drops_plan_pointer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", "clear-sess")
    store.record_active_plan("some-task-id")
    store.record_clear()
    loaded = SessionStore.load(store.path)
    assert loaded.active_plan_task_id is None


# ---------------------------------------------------------------------------
# Test 11: step-progress dedupe — no extra identical pointer records
# ---------------------------------------------------------------------------


def test_step_progress_no_extra_pointer_records(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", "dedup-sess")
    fake = FakeLLM(
        script=[
            plan_call(),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            answer("All done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path, session=store)
    runtime.run_turn("do it")

    raw_lines = [json.loads(line) for line in store.path.read_text().splitlines() if line.strip()]
    active_plan_records = [rec for rec in raw_lines if rec.get("type") == "active_plan"]

    # There should be exactly ONE non-null active_plan record (at approve time)
    # and ONE null record (at completion). No duplicates in between for each step update.
    non_null = [r for r in active_plan_records if r.get("task_id") is not None]
    assert len(non_null) == 1  # only the approval transition


# ---------------------------------------------------------------------------
# Test 12: different workspace → no restore
# ---------------------------------------------------------------------------


def test_different_workspace_no_restore(tmp_path: Path) -> None:
    from shellpilot.runtime.planner import PlanManager

    # Create plan in workspace A
    workspace_a = tmp_path / "ws_a"
    workspace_a.mkdir()
    manager = PlanManager(workspace_a, "balanced")
    plan = manager.create(goal="g", user_intent="u", steps=["A"], assumptions=[], verification=[])
    manager.approve()
    task_id = plan.task_id

    # Try to restore in workspace B
    workspace_b = tmp_path / "ws_b"
    workspace_b.mkdir()
    store = SessionStore(workspace_b / "sessions", "sess")
    runtime = make_runtime(FakeLLM(script=[]), FakeUI(), workspace_b, session=store)
    runtime.restore_active_plan(task_id)
    assert runtime.plan_manager.active is None  # different workspace → no state.json found
