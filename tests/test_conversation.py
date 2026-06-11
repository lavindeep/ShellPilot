"""Tests for the unified conversation runtime with the fake model."""

import json
from pathlib import Path

from shellpilot.config.model import ContextSettings, Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_runtime(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, settings: Settings | None = None
) -> ConversationRuntime:
    from shellpilot.memory.agents_md import BehaviorInstructions

    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )


def test_run_turn_returns_answer_and_streams(tmp_path: Path) -> None:
    fake = FakeLLM(script=[answer("Paris is the capital of France.")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("What is the capital of France?")

    assert reply == "Paris is the capital of France."
    assert "".join(ui.tokens) == reply
    call = fake.calls[0]
    assert call.messages[0].role == "system"
    assert call.messages[-1].content == "What is the capital of France?"
    assert call.num_ctx == runtime.budget.model_context_tokens


def test_history_accumulates_across_turns(tmp_path: Path) -> None:
    fake = FakeLLM(script=[answer("one"), answer("two")])
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    runtime.run_turn("first")
    runtime.run_turn("second")
    second_call = fake.calls[1]
    contents = [message.content for message in second_call.messages]
    assert "first" in contents
    assert "one" in contents
    assert "second" in contents


def test_oversized_user_message_is_refused(tmp_path: Path) -> None:
    fake = FakeLLM(script=[])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)
    huge = "x" * (runtime.budget.max_user_message_tokens * 4 + 100)

    reply = runtime.run_turn(huge)

    assert reply == ""
    assert fake.calls == []  # never reached the model
    assert any("file" in status.lower() for status in ui.statuses)


def test_compaction_drops_oldest_turns(tmp_path: Path) -> None:
    # Tiny explicit context so a few turns cross the threshold.
    settings = Settings(context=ContextSettings(model_context_tokens=512))
    script = [answer(f"reply {i} " + "pad " * 30) for i in range(6)]
    fake = FakeLLM(script=script)
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path, settings)

    for i in range(6):
        runtime.run_turn(f"question {i} " + "pad " * 30)

    assert any("Compacted" in status for status in ui.statuses)
    final_call = fake.calls[-1]
    contents = [message.content for message in final_call.messages]
    assert not any("question 0" in content for content in contents)


def test_set_model_refreshes_budget(tmp_path: Path) -> None:
    fake = FakeLLM(script=[], context_length=8192)
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    assert runtime.budget.model_context_tokens == 8192
    fake.context_length = 32768
    runtime.set_model("gemma4:e2b")
    assert runtime.model == "gemma4:e2b"
    assert runtime.budget.model_context_tokens == 32768


def test_status_snapshot(tmp_path: Path) -> None:
    fake = FakeLLM(script=[answer("hi")])
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    runtime.run_turn("hello")
    status = runtime.status()
    assert status.model == "gemma4:e4b"
    assert status.profile == "balanced"
    assert status.history_messages == 2
    assert status.estimated_prompt_tokens > 0


# ---------------------------------------------------------------------------
# A4: clear_history resets plan, snapshots, diffs, and failure state
# ---------------------------------------------------------------------------


def _make_runtime_with_audit(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, audit: AuditLogger
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        audit=audit,
    )


def test_clear_cancels_active_plan(tmp_path: Path) -> None:
    """clear_history() cancels the active plan and clears plan_manager.active."""
    fake = FakeLLM(
        script=[
            tool_call(
                "propose_plan",
                goal="Do something",
                steps=["Step one", "Step two", "Step three"],
                assumptions=[],
                verification=[],
            ),
            # plain-text answers stop the nudge loop (MAX_PLAN_NUDGES=2)
            answer("Starting step 1."),
            answer("Still on step 1."),
            answer("Stopping for now."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.run_turn("Do the thing")

    assert runtime.plan_manager.active is not None
    plan = runtime.plan_manager.active
    artifact = runtime.plan_manager.artifact_path(plan)

    runtime.clear_history()

    assert runtime.plan_manager.active is None
    # cancel() writes "cancelled" status to the artifact
    assert "cancelled" in artifact.read_text().lower()


def test_clear_resets_snapshots_and_diffs(tmp_path: Path) -> None:
    """clear_history() empties snapshots, recent_diffs, and _last_failure_signature."""
    fake = FakeLLM(script=[])
    runtime = make_runtime(fake, FakeUI(), tmp_path)

    # Seed a snapshot manually (simulating a prior read_file call)
    some_file = tmp_path / "hello.txt"
    some_file.write_bytes(b"hello")
    runtime.snapshots.record(some_file, b"hello")
    assert len(runtime.snapshots) == 1

    # Seed a recent diff and a failure signature
    runtime.recent_diffs.append("diff --git a/x b/x\n")
    runtime._last_failure_signature = "read_file:file not found"

    runtime.clear_history()

    assert len(runtime.snapshots) == 0
    assert runtime.recent_diffs == []
    assert runtime._last_failure_signature is None


def test_clear_writes_audit_event(tmp_path: Path) -> None:
    """clear_history() appends a 'clear' event to the audit log."""
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-clear",
        workspace=tmp_path,
        profile="balanced",
    )
    fake = FakeLLM(script=[])
    ui = FakeUI()
    runtime = _make_runtime_with_audit(fake, ui, tmp_path, audit)

    runtime.clear_history()

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    clear_events = [e for e in events if e["event"] == "clear"]
    assert len(clear_events) == 1
    assert "plan" in clear_events[0].get("summary", "").lower()


def test_update_plan_after_clear_reports_no_active_plan(tmp_path: Path) -> None:
    """After clear, the update_plan tool handler returns 'no active plan'."""
    fake = FakeLLM(
        script=[
            tool_call(
                "propose_plan",
                goal="Do something",
                steps=["Step one", "Step two", "Step three"],
                assumptions=[],
                verification=[],
            ),
            # Exhaust the two nudge replies, then a plain-text ending
            answer("Still on step 1."),
            answer("Still on step 1."),
            answer("Stopping for now."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.run_turn("Do the thing")
    runtime.clear_history()

    # Call the update_plan tool handler directly (no LLM call needed)
    from shellpilot.tools.base import ToolContext

    update_spec = next(s for s in runtime.registry.specs() if s.name == "update_plan")
    ctx = ToolContext(workspace=tmp_path, max_result_tokens=4096)
    result = update_spec.handler(ctx, {"step": 1, "status": "completed"})

    assert not result.success
    assert "no active plan" in result.summary.lower()
