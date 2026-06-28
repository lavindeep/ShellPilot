"""Tests for the unified conversation runtime with the fake model."""

import json
import threading
from pathlib import Path

import pytest

from shellpilot.config.model import (
    ContextSettings,
    PrivacySettings,
    Settings,
    SkillSettings,
    ToolSettings,
)
from shellpilot.llm.client import GenerationCancelled
from shellpilot.llm.messages import Message, ToolDefinition
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.skills.loader import discover_skills
from shellpilot.skills.model import Skill, SkillTrigger
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec
from shellpilot.tools.registry import ToolRegistry
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


def test_cancelled_turn_discards_partial_reply(tmp_path: Path) -> None:
    """History integrity (§31.15): a cancelled turn keeps the user message but no reply."""
    # FakeLLM raises GenerationCancelled when the cancel event is set, mirroring
    # OllamaClient aborting mid-stream. The scripted answer must NEVER be recorded.
    fake = FakeLLM(script=[answer("partial reply that must never be recorded")])
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(GenerationCancelled):
        runtime.run_turn("do a thing", cancel=cancel)

    # The user message was recorded (the turn happened) ...
    assert [m.role for m in runtime._history] == ["user"]
    assert runtime._history[0].content == "do a thing"
    # ... but NO assistant reply landed in history — the partial is gone.
    assert all(m.role != "assistant" for m in runtime._history)


def test_cancel_during_tool_execution_aborts(tmp_path: Path) -> None:
    """Branch 6b (§31.15): a Ctrl-C landing during tool execution aborts the turn.

    A fake tool sets the turn's cancel event via its ToolContext (simulating a
    long run_command child being killed mid-execution). The tool loop then raises
    GenerationCancelled, so the cancelled tool's RESULT is never recorded and the
    model is not re-invoked.
    """
    cancel_seen: list[bool] = []

    def _set_cancel(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        cancel_seen.append(context.cancel is not None)
        assert context.cancel is not None  # the turn's cancel threaded to the handler
        context.cancel.set()
        return ToolResult(success=True, summary="cancelled mid-run", content="discarded")

    spec = ToolSpec(
        definition=ToolDefinition(name="slow_tool", description="d", parameters={}, required=()),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_set_cancel,
    )
    registry = ToolRegistry()
    registry.register(spec)

    fake = FakeLLM(script=[tool_call("slow_tool"), answer("must never be recorded")])
    runtime = ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        registry=registry,
    )
    cancel = threading.Event()  # starts UNSET; the tool sets it

    with pytest.raises(GenerationCancelled):
        runtime.run_turn("go", cancel=cancel)

    assert cancel_seen == [True]  # the handler saw the cancel event
    # The cancelled model step is rolled fully out of history: only the user
    # message remains — no orphaned assistant tool_call (an assistant reply whose
    # tool_call has no matching result) survives to be re-sent next turn, and no
    # tool result is recorded. Matches the model-stream cancel's clean discard,
    # which never records its partial reply.
    assert [m.role for m in runtime._history] == ["user"]
    assert runtime._history[0].content == "go"
    assert all(not m.tool_calls for m in runtime._history)
    # ... and no follow-up answer landed — the model was not re-invoked.
    assert all("must never be recorded" not in m.content for m in runtime._history)
    assert len(fake.calls) == 1


def test_normal_turn_records_assistant_reply(tmp_path: Path) -> None:
    """Control for the cancel test: an uncancelled turn still records the reply."""
    fake = FakeLLM(script=[answer("the answer")])
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    cancel = threading.Event()  # never set

    runtime.run_turn("question", cancel=cancel)

    assert [m.role for m in runtime._history] == ["user", "assistant"]
    assert runtime._history[1].content == "the answer"


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


def _web_grounding_skill() -> Skill:
    return Skill(
        name="web-grounding",
        description="Web guidance.",
        body="Use web only when needed.",
        root="builtin",
        triggers=(SkillTrigger.WEB_ENABLED,),
        est_tokens=6,
    )


def _real_builtin_skills() -> tuple[Skill, ...]:
    return tuple(discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800))


def _injected_block_texts(runtime: ConversationRuntime) -> dict[str, str]:
    return {block.name: block.text for block in runtime.context_snapshot().blocks if block.injected}


def test_web_enabled_trigger_uses_registered_tools(tmp_path: Path) -> None:
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(tools=ToolSettings(web=True)),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=(_web_grounding_skill(),),
    )

    snapshot = runtime.context_snapshot()

    decision = snapshot.decisions[0]
    assert decision.skill == "web-grounding"
    assert decision.injected is True
    assert decision.matched_triggers == (SkillTrigger.WEB_ENABLED,)


def test_web_setting_drift_without_registered_tools_does_not_fire(tmp_path: Path) -> None:
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=(_web_grounding_skill(),),
    )
    runtime.update_settings(Settings(tools=ToolSettings(web=True)))

    snapshot = runtime.context_snapshot()

    decision = snapshot.decisions[0]
    assert decision.skill == "web-grounding"
    assert decision.injected is False
    assert decision.reason == "web disabled"


def test_plan_proposed_trigger_fires_without_plan_state_block(tmp_path: Path) -> None:
    proposed_skill = Skill(
        name="planning",
        description="Planning guidance.",
        body="Propose a concise plan.",
        root="builtin",
        triggers=(SkillTrigger.PLAN_PROPOSED,),
        est_tokens=6,
    )
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=(proposed_skill,),
    )
    runtime.plan_manager.create(
        goal="ship",
        user_intent="ship",
        steps=["check"],
        assumptions=[],
        verification=[],
    )

    snapshot = runtime.context_snapshot()

    decision = snapshot.decisions[0]
    assert decision.injected is True
    assert decision.matched_triggers == (SkillTrigger.PLAN_PROPOSED,)
    plan_state = next(block for block in snapshot.blocks if block.name == "plan state")
    assert plan_state.injected is False


def test_planning_builtin_references_follow_plan_mode(tmp_path: Path) -> None:
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=_real_builtin_skills(),
    )
    plan = runtime.plan_manager.create(
        goal="ship",
        user_intent="ship",
        steps=["check", "change", "verify"],
        assumptions=[],
        verification=[],
    )

    proposed_blocks = _injected_block_texts(runtime)
    assert plan.status == "proposed"
    assert "propose_plan once" in proposed_blocks["skill:planning:reference:proposed.md"]
    assert "skill:planning:reference:active.md" not in proposed_blocks
    assert "skill:planning:reference:blocked.md" not in proposed_blocks

    runtime.plan_manager.approve()
    active_blocks = _injected_block_texts(runtime)
    assert plan.status == "active"
    assert "update_plan(step=N," in active_blocks["skill:planning:reference:active.md"]
    assert "skill:planning:reference:proposed.md" not in active_blocks
    assert "skill:planning:reference:blocked.md" not in active_blocks

    runtime.plan_manager.record_blocker("dependency missing")
    blocked_blocks = _injected_block_texts(runtime)
    assert plan.status == "blocked"
    assert (
        'update_plan(blocker="<evidence>")' in blocked_blocks["skill:planning:reference:blocked.md"]
    )
    assert "skill:planning:reference:proposed.md" not in blocked_blocks
    assert "skill:planning:reference:active.md" not in blocked_blocks


def test_skill_authoring_builtin_inactive_unless_enabled(tmp_path: Path) -> None:
    disabled = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=_real_builtin_skills(),
    )

    disabled_snapshot = disabled.context_snapshot()
    disabled_decision = next(d for d in disabled_snapshot.decisions if d.skill == "skill-authoring")
    assert disabled_decision.injected is False
    assert disabled_decision.reason == "disabled"
    assert "## Skill: skill-authoring" not in disabled_snapshot.system_text()

    enabled = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(skills=SkillSettings(enabled=("skill-authoring",))),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        skills=_real_builtin_skills(),
    )

    enabled_snapshot = enabled.context_snapshot()
    enabled_decision = next(d for d in enabled_snapshot.decisions if d.skill == "skill-authoring")
    assert enabled_decision.injected is True
    assert enabled_decision.matched_triggers == (SkillTrigger.ENABLED,)
    assert "## Skill: skill-authoring" in enabled_snapshot.system_text()


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
    """clear_history() empties snapshots, recent_diffs, failure sig, and _last_user_text."""
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

    # Seed _last_user_text directly (same precedent as _last_failure_signature above)
    runtime._last_user_text = "do something important"
    assert runtime._last_user_text == "do something important"

    runtime.clear_history()

    assert len(runtime.snapshots) == 0
    assert runtime.recent_diffs == []
    assert runtime._last_failure_signature is None
    assert runtime._last_user_text == ""


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


def test_user_turn_audit_counts_images(tmp_path: Path) -> None:
    """The user_turn audit event carries images=N only when images are passed."""
    import base64
    import hashlib

    from shellpilot.llm.messages import ImageRef
    from tests.conftest import TINY_PNG

    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-img",
        workspace=tmp_path,
        profile="balanced",
    )
    ref = ImageRef(
        path=str(tmp_path / "shot.png"),
        sha256=hashlib.sha256(TINY_PNG).hexdigest(),
        data_b64=base64.b64encode(TINY_PNG).decode(),
        size_bytes=len(TINY_PNG),
    )
    fake = FakeLLM(script=[answer("with image"), answer("without image")])
    ui = FakeUI()
    runtime = _make_runtime_with_audit(fake, ui, tmp_path, audit)

    runtime.run_turn("describe this", images=(ref,))
    runtime.run_turn("plain text only")

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    user_turns = [e for e in events if e["event"] == "user_turn"]
    assert len(user_turns) == 2
    assert user_turns[0].get("images") == 1
    # A turn without images carries no images field (absent / 0).
    assert user_turns[1].get("images", 0) == 0


# ---------------------------------------------------------------------------
# B9: run_turn images= + image token estimate
# ---------------------------------------------------------------------------


def test_run_turn_passes_images_into_user_message(tmp_path: Path) -> None:
    """run_turn(text, images=...) records a user message carrying the ImageRef."""
    import base64
    import hashlib

    from shellpilot.llm.messages import ImageRef
    from tests.conftest import TINY_PNG

    data_b64 = base64.b64encode(TINY_PNG).decode()
    ref = ImageRef(
        path=str(tmp_path / "sample.png"),
        sha256=hashlib.sha256(TINY_PNG).hexdigest(),
        data_b64=data_b64,
        size_bytes=len(TINY_PNG),
    )

    fake = FakeLLM(script=[answer("I see a white pixel.")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("What is in this image?", images=(ref,))

    # The FakeLLM received a message containing the image ref
    call = fake.calls[0]
    user_messages = [m for m in call.messages if m.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].images == (ref,)

    # And it is in the recorded history too
    history_user = [m for m in runtime._history if m.role == "user"]
    assert len(history_user) == 1
    assert history_user[0].images == (ref,)


def test_image_token_estimate_counted(tmp_path: Path) -> None:
    """Messages with images contribute IMAGE_TOKEN_ESTIMATE tokens to the estimate."""
    import base64
    import hashlib

    from shellpilot.llm.messages import ImageRef
    from shellpilot.runtime.conversation import IMAGE_TOKEN_ESTIMATE
    from tests.conftest import TINY_PNG

    data_b64 = base64.b64encode(TINY_PNG).decode()
    ref = ImageRef(
        path="/tmp/img.png",
        sha256=hashlib.sha256(TINY_PNG).hexdigest(),
        data_b64=data_b64,
        size_bytes=len(TINY_PNG),
    )

    fake_no_image = FakeLLM(script=[answer("plain reply")])
    runtime_no_image = make_runtime(fake_no_image, FakeUI(), tmp_path)
    runtime_no_image.run_turn("hello, no image")
    estimate_no_image = runtime_no_image.estimated_prompt_tokens()

    fake_with_image = FakeLLM(script=[answer("image reply")])
    runtime_with_image = make_runtime(fake_with_image, FakeUI(), tmp_path)
    runtime_with_image.run_turn("hello, with image", images=(ref,))
    estimate_with_image = runtime_with_image.estimated_prompt_tokens()

    assert estimate_with_image >= estimate_no_image + IMAGE_TOKEN_ESTIMATE


def test_set_workspace_rebuilds_project_memory(tmp_path: Path) -> None:
    """Changing workspace (/cwd) rebuilds the project memory store for the new
    path, so the previous workspace's facts stop injecting (design section 16);
    the shared global store is preserved."""
    from shellpilot.memory.store import MemoryStore, MemoryStores, project_id_for
    from shellpilot.persistence.paths import project_state_dir

    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    global_store = MemoryStore(tmp_path / "global-memory.json")
    stores = MemoryStores(
        global_store=global_store,
        project_store=MemoryStore(
            project_state_dir(workspace_a) / "memory.json",
            project_id=project_id_for(workspace_a),
        ),
    )
    stores.project_store.add_fact(kind="config", value="postgres-a", label="db", source="user")

    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=workspace_a,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        memory=stores,
    )
    assert "postgres-a" in runtime.context_snapshot().system_text()

    runtime.set_workspace(workspace_b)

    rendered_b = runtime.context_snapshot().system_text()
    assert "postgres-a" not in rendered_b  # A's fact no longer injected into B
    assert runtime.memory is not None
    assert runtime.memory.global_store is global_store  # shared global untouched

    runtime.memory.project_store.add_fact(
        kind="config", value="postgres-b", label="db", source="user"
    )
    assert "postgres-b" in runtime.context_snapshot().system_text()


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


# ---------------------------------------------------------------------------
# Empty-response nudge: a reasoning-only / empty reply after a tool result is
# nudged to keep going instead of silently ending the turn.
# ---------------------------------------------------------------------------


def _seed_file(tmp_path: Path) -> Path:
    target = tmp_path / "seed.txt"
    target.write_text("hello world\n", encoding="utf-8")
    return target


def test_empty_reply_after_tool_call_is_nudged(tmp_path: Path) -> None:
    """An empty reply after a tool result is nudged; the next answer is returned."""
    _seed_file(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="seed.txt"),
            answer(""),  # reasoning-only / empty turn
            answer("The file says hello world."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("read seed.txt")

    assert reply == "The file says hello world."
    assert len(fake.calls) == 3
    # A nudge tool_result was recorded between the empty reply and the answer.
    from shellpilot.runtime.conversation import EMPTY_CONTINUE_NUDGE

    tool_msgs = [m.content for m in runtime._history if m.role == "tool"]
    assert any(EMPTY_CONTINUE_NUDGE == c for c in tool_msgs)
    # Specifically the "already run a tool" wording, not the first-reply variant.
    assert any("already run a tool" in c for c in tool_msgs)


def test_whitespace_only_reply_is_nudged(tmp_path: Path) -> None:
    """A whitespace-only middle reply is treated as empty and nudged."""
    _seed_file(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="seed.txt"),
            answer("   \n\t "),
            answer("Done."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("read seed.txt")

    assert reply == "Done."
    assert len(fake.calls) == 3


def test_empty_reply_budget_exhaustion_stops(tmp_path: Path) -> None:
    """After MAX_EMPTY_NUDGES the loop stops; status mentions the empty response."""
    from shellpilot.runtime.conversation import MAX_EMPTY_NUDGES

    _seed_file(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="seed.txt"),
            *[answer("") for _ in range(MAX_EMPTY_NUDGES + 1)],
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("read seed.txt")

    assert reply == ""
    # 1 tool call + (MAX_EMPTY_NUDGES + 1) empty replies = nudges then exhaustion.
    assert len(fake.calls) == 1 + MAX_EMPTY_NUDGES + 1
    assert any("(empty response)" in status for status in ui.statuses)


def test_empty_first_reply_is_nudged(tmp_path: Path) -> None:
    """An empty FIRST reply (no tool ran yet) is nudged with EMPTY_FIRST_NUDGE."""
    from shellpilot.runtime.conversation import EMPTY_FIRST_NUDGE

    fake = FakeLLM(script=[answer(""), answer("Here is my answer.")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("hello")

    assert reply == "Here is my answer."
    assert len(fake.calls) == 2
    # A nudge tool_result using EMPTY_FIRST_NUDGE was recorded between the replies.
    tool_msgs = [m.content for m in runtime._history if m.role == "tool"]
    assert any(EMPTY_FIRST_NUDGE == c for c in tool_msgs)
    assert not any("(empty response)" in status for status in ui.statuses)


def test_empty_first_reply_budget_exhaustion(tmp_path: Path) -> None:
    """After MAX_EMPTY_NUDGES from the very first reply, show (empty response)."""
    from shellpilot.runtime.conversation import MAX_EMPTY_NUDGES

    fake = FakeLLM(script=[answer("") for _ in range(MAX_EMPTY_NUDGES + 1)])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("hello")

    assert reply == ""
    assert len(fake.calls) == MAX_EMPTY_NUDGES + 1
    assert any("(empty response)" in status for status in ui.statuses)


def test_empty_reply_writes_audit_events(tmp_path: Path) -> None:
    """The nudge and the exhaustion both emit audit events."""
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-empty",
        workspace=tmp_path,
        profile="balanced",
    )
    _seed_file(tmp_path)
    from shellpilot.runtime.conversation import MAX_EMPTY_NUDGES

    fake = FakeLLM(
        script=[
            tool_call("read_file", path="seed.txt"),
            *[answer("") for _ in range(MAX_EMPTY_NUDGES + 1)],
        ]
    )
    ui = FakeUI()
    runtime = _make_runtime_with_audit(fake, ui, tmp_path, audit)

    runtime.run_turn("read seed.txt")

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    nudge_events = [e for e in events if e["event"] == "empty_response_nudge"]
    final_events = [e for e in events if e["event"] == "empty_response"]
    assert len(nudge_events) == MAX_EMPTY_NUDGES
    assert len(final_events) == 1


def test_plan_nudge_takes_priority_over_empty_nudge(tmp_path: Path) -> None:
    """With an active approved plan, an empty reply fires the plan nudge first."""
    fake = FakeLLM(
        script=[
            tool_call(
                "propose_plan",
                goal="Do something",
                steps=["Step one", "Step two"],
                assumptions=[],
                verification=[],
            ),
            answer(""),  # empty: plan still active, so plan nudge must win
            answer("Still working."),  # second no-tool reply, also plan-nudged
            answer("Stopping for now."),  # third reply ends the turn (MAX_PLAN_NUDGES=2)
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("Do the thing")

    from shellpilot.runtime.conversation import EMPTY_CONTINUE_NUDGE, PLAN_CONTINUE_NUDGE

    tool_msgs = [m.content for m in runtime._history if m.role == "tool"]
    plan_prefix = PLAN_CONTINUE_NUDGE.split("{")[0]
    # The plan nudge fired for the empty reply; the empty-response nudge did not.
    assert any(c.startswith(plan_prefix) for c in tool_msgs)
    assert EMPTY_CONTINUE_NUDGE not in tool_msgs


def _complete_last_step_reply(content: str) -> Message:
    """Reply that narrates ``content`` AND marks the plan's final step completed."""
    from shellpilot.llm.messages import ToolCall, assistant

    return assistant(
        content,
        tool_calls=(ToolCall(name="update_plan", arguments={"step": 2, "status": "completed"}),),
    )


_SUMMARY = (
    "Done. I created the directory, wrote the config file, and verified the "
    "service starts cleanly with the new settings."
)


def _two_step_plan_at_final_step(fake: FakeLLM, ui: FakeUI, tmp_path: Path) -> ConversationRuntime:
    """Build a runtime with an approved two-step plan whose step 1 is completed.

    Step 2 is left active so the model's next ``update_plan(step=2, completed)``
    transitions the whole plan to completed.
    """
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.plan_manager.create(
        goal="Set up the service",
        user_intent="set up the service",
        steps=["Step one", "Step two"],
        assumptions=[],
        verification=[],
    )
    runtime.plan_manager.approve()
    runtime.plan_manager.update_step(1, "completed")
    return runtime


def test_substantive_summary_after_explicit_completion_is_not_re_invoked(
    tmp_path: Path,
) -> None:
    """A final-step completing reply that already summarizes ends the turn.

    The plan transitions to completed via the model's own update_plan call, and
    the streamed prose IS the single summary — the model is NOT re-invoked on
    the planner's end-of-plan summary prompt.
    """
    fake = FakeLLM(script=[_complete_last_step_reply(_SUMMARY)])
    ui = FakeUI(plan_answer=("y", ""))
    runtime = _two_step_plan_at_final_step(fake, ui, tmp_path)

    reply = runtime.run_turn("finish the task")

    assert runtime.plan_manager.active is not None
    assert runtime.plan_manager.active.status == "completed"
    # Exactly one chat call: the completing reply. No second round for the
    # planner's end-of-plan summary prompt.
    assert len(fake.calls) == 1
    assert reply == _SUMMARY


def test_short_completion_reply_still_prompts_for_summary(tmp_path: Path) -> None:
    """A completing reply with short/empty content is re-invoked for the summary.

    With no substantive summary in the completing reply, the planner's end-of-plan
    summary prompt still fires, eliciting the single summary turn.
    """
    fake = FakeLLM(
        script=[
            _complete_last_step_reply("done"),  # below MIN_SUMMARY_CHARS
            answer(_SUMMARY),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = _two_step_plan_at_final_step(fake, ui, tmp_path)

    reply = runtime.run_turn("finish the task")

    assert runtime.plan_manager.active is not None
    assert runtime.plan_manager.active.status == "completed"
    # Two chat calls: the terse completion, then the model re-invoked on the
    # planner's end-of-plan summary prompt.
    assert len(fake.calls) == 2
    assert reply == _SUMMARY


def test_non_final_step_completion_still_nudges(tmp_path: Path) -> None:
    """Completing a non-final step (plan not done) keeps stall recovery intact.

    A no-tool-call reply while a later step is still pending fires the plan
    continue-nudge rather than suppressing anything.
    """
    from shellpilot.runtime.conversation import PLAN_CONTINUE_NUDGE

    fake = FakeLLM(
        script=[
            answer("I will keep going."),  # no tool call, plan not done -> nudge
            answer("Still working on it."),  # nudged again (MAX_PLAN_NUDGES=2)
            answer("Stopping for now."),  # third reply ends the turn
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.plan_manager.create(
        goal="Set up the service",
        user_intent="set up the service",
        steps=["Step one", "Step two"],
        assumptions=[],
        verification=[],
    )
    runtime.plan_manager.approve()  # step 1 active, step 2 pending — plan not done

    runtime.run_turn("continue")

    tool_msgs = [m.content for m in runtime._history if m.role == "tool"]
    plan_prefix = PLAN_CONTINUE_NUDGE.split("{")[0]
    assert any(c.startswith(plan_prefix) for c in tool_msgs)


def test_empty_reply_still_routes_to_empty_nudge(tmp_path: Path) -> None:
    """An empty reply (no content, no tool calls) still hits the empty-reply path."""
    from shellpilot.runtime.conversation import EMPTY_FIRST_NUDGE

    fake = FakeLLM(script=[answer(""), answer("Here is my answer.")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("hello")

    assert reply == "Here is my answer."
    tool_msgs = [m.content for m in runtime._history if m.role == "tool"]
    assert any(EMPTY_FIRST_NUDGE == c for c in tool_msgs)


def test_plan_continue_nudge_is_positive_routing_not_a_muzzle() -> None:
    """The continue-nudge routes via update_plan and contains no muzzle wording."""
    from shellpilot.runtime.conversation import PLAN_CONTINUE_NUDGE

    text = PLAN_CONTINUE_NUDGE
    assert "update_plan(step=" in text
    assert 'status="completed"' in text
    lowered = text.lower()
    assert "do not repeat" not in lowered
    assert "don't repeat" not in lowered
    assert "do not narrate" not in lowered


def test_runtime_forwards_model_options_to_chat(tmp_path: Path) -> None:
    """The runtime passes settings.model.options through to the chat call."""
    from shellpilot.config.model import ModelSettings

    settings = Settings(model=ModelSettings(options={"repeat_penalty": 1.3}))
    fake = FakeLLM(script=[answer("hi")])
    runtime = make_runtime(fake, FakeUI(), tmp_path, settings)

    runtime.run_turn("hello")

    assert fake.calls[0].options == {"repeat_penalty": 1.3}


# ---------------------------------------------------------------------------
# skill_read registration gating (section 23.4)
# ---------------------------------------------------------------------------


def test_skill_read_absent_when_skills_disabled(tmp_path: Path) -> None:
    """Default settings (skills.enabled=()) → skill_read not registered."""
    fake = FakeLLM(script=[])
    runtime = make_runtime(fake, FakeUI(), tmp_path, settings=Settings())
    assert runtime.registry.get("skill_read") is None


def test_skill_read_registered_when_skills_enabled(tmp_path: Path) -> None:
    """Non-empty skills.enabled → skill_read registered in the runtime."""
    fake = FakeLLM(script=[])
    settings = Settings(skills=SkillSettings(enabled=("skill-authoring",)))
    runtime = make_runtime(fake, FakeUI(), tmp_path, settings=settings)
    assert runtime.registry.get("skill_read") is not None


# ---------------------------------------------------------------------------
# Group E: egress chokepoint (locality signal, outbound redaction, model_request)
# ---------------------------------------------------------------------------


def _make_runtime_with_base_url(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    base_url: str,
    *,
    audit: AuditLogger | None = None,
    settings: Settings | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        base_url=base_url,
        audit=audit,
    )


def test_endpoint_loopback_detection_local(tmp_path: Path) -> None:
    """localhost / 127.x / ::1 / 0.0.0.0 / empty host are loopback (not egressing)."""
    from shellpilot.llm.ollama import is_loopback_url

    for url in (
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.5.6.7:11434",
        "http://[::1]:11434",
        "http://0.0.0.0:11434",
        "http://foo.localhost:11434",
    ):
        runtime = _make_runtime_with_base_url(FakeLLM(script=[]), FakeUI(), tmp_path, url)
        assert is_loopback_url(url) is True, url
        assert runtime._is_egressing() is False, url


def test_endpoint_loopback_detection_remote(tmp_path: Path) -> None:
    """A non-loopback host is remote → egressing."""
    from shellpilot.llm.ollama import is_loopback_url

    for url in (
        "https://ollama.com",
        "https://api.example.com:443",
        "http://10.0.0.5:11434",  # private but not loopback → still off this box
    ):
        runtime = _make_runtime_with_base_url(FakeLLM(script=[]), FakeUI(), tmp_path, url)
        assert is_loopback_url(url) is False, url
        assert runtime._is_egressing() is True, url


def test_default_base_url_is_loopback(tmp_path: Path) -> None:
    """The default constructor (no base_url) is loopback — zero behaviour change."""
    runtime = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path)
    assert runtime._is_egressing() is False


def test_outbound_redaction_on_remote_turn(tmp_path: Path) -> None:
    """A secret in history is redacted in the messages handed to chat() on a remote turn."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    fake = FakeLLM(script=[answer("done")])
    runtime = _make_runtime_with_base_url(fake, FakeUI(), tmp_path, "https://ollama.com")
    # Seed history with a tool result carrying a secret.
    from shellpilot.llm.messages import tool_result

    runtime._history.append(tool_result(f"key is {secret}"))

    runtime.run_turn("summarize")

    sent = fake.calls[0].messages
    joined = "\n".join(m.content for m in sent)
    assert secret not in joined
    assert "[REDACTED]" in joined
    # History itself is never mutated.
    assert any(secret in m.content for m in runtime._history)


def test_loopback_turn_messages_byte_identical(tmp_path: Path) -> None:
    """On a loopback turn the messages are passed unchanged — redaction is never invoked."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    fake = FakeLLM(script=[answer("done")])
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    from shellpilot.llm.messages import tool_result

    runtime._history.append(tool_result(f"key is {secret}"))

    # Positively pin the no-copy path: the egress redaction helper must not run
    # on a loopback turn (the remote test proves it DOES run when egressing).
    called = False
    original = runtime._redacted_for_egress

    def _spy(messages):
        nonlocal called
        called = True
        return original(messages)

    runtime._redacted_for_egress = _spy

    runtime.run_turn("summarize")

    assert called is False  # loopback never invokes outbound redaction
    sent = fake.calls[0].messages
    joined = "\n".join(m.content for m in sent)
    assert secret in joined  # not redacted — byte-identical
    assert any(secret in m.content for m in runtime._history)


def test_outbound_redaction_disabled_when_privacy_off(tmp_path: Path) -> None:
    """redact_secrets=False → even remote turns are not redacted."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    fake = FakeLLM(script=[answer("done")])
    settings = Settings(privacy=PrivacySettings(redact_secrets=False))
    runtime = _make_runtime_with_base_url(
        fake, FakeUI(), tmp_path, "https://ollama.com", settings=settings
    )
    from shellpilot.llm.messages import tool_result

    runtime._history.append(tool_result(f"key is {secret}"))

    runtime.run_turn("summarize")

    joined = "\n".join(m.content for m in fake.calls[0].messages)
    assert secret in joined


def test_model_request_audit_on_remote_turn(tmp_path: Path) -> None:
    """A remote turn writes a model_request event with host/model/counts and NO body."""
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-egress",
        workspace=tmp_path,
        profile="balanced",
    )
    fake = FakeLLM(script=[answer("done")])
    runtime = _make_runtime_with_base_url(
        fake, FakeUI(), tmp_path, "https://ollama.com", audit=audit
    )
    from shellpilot.llm.messages import tool_result

    runtime._history.append(tool_result("some prompt body text here"))

    runtime.run_turn("hello")

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    reqs = [e for e in events if e["event"] == "model_request"]
    assert len(reqs) == 1
    ev = reqs[0]
    assert ev["host"] == "ollama.com"
    assert ev["model"] == "gemma4:e4b"
    assert ev["locality"] == "remote"
    assert ev["message_count"] >= 1
    assert ev["approx_bytes"] > 0
    assert ev["image_count"] == 0
    # No message body is recorded under any field.
    assert "some prompt body text here" not in json.dumps(ev)
    assert "hello" not in json.dumps(ev)


def test_no_model_request_audit_on_loopback_turn(tmp_path: Path) -> None:
    """A loopback turn writes no model_request event."""
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="sess-local",
        workspace=tmp_path,
        profile="balanced",
    )
    fake = FakeLLM(script=[answer("done")])
    runtime = _make_runtime_with_audit(fake, FakeUI(), tmp_path, audit)
    runtime.run_turn("hello")

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert not any(e["event"] == "model_request" for e in events)


# ---------------------------------------------------------------------------
# Shared loopback helper + cloud-model egress (v0.10.0 Part 2)
# ---------------------------------------------------------------------------


def test_is_loopback_url_shared_helper() -> None:
    """The module-level helper classifies loopback vs remote URLs consistently."""
    from shellpilot.llm.ollama import is_loopback_url

    for url in (
        "",
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.5.6.7:11434",
        "http://[::1]:11434",
        "http://0.0.0.0:11434",
        "http://foo.localhost:11434",
    ):
        assert is_loopback_url(url) is True, url
    for url in (
        "https://ollama.com",
        "https://api.example.com:443",
        "http://10.0.0.5:11434",
        "ollama.com:443",  # scheme-less, no parseable host → fail closed (remote)
        "http://[bad",  # unparseable URL → fail closed (remote), no raise
    ):
        assert is_loopback_url(url) is False, url


def _make_runtime_with_model(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, model: str, *, base_url: str
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        model=model,
        base_url=base_url,
    )


def test_cloud_model_egresses_on_localhost(tmp_path: Path) -> None:
    """A '-cloud' model egresses even through a loopback Ollama proxy."""
    from shellpilot.llm.ollama import is_loopback_url

    runtime = _make_runtime_with_model(
        FakeLLM(script=[]),
        FakeUI(),
        tmp_path,
        "nemotron-3-nano:30b-cloud",
        base_url="http://localhost:11434",
    )
    assert is_loopback_url("http://localhost:11434") is True
    assert runtime._is_egressing() is True


def test_local_model_on_localhost_does_not_egress(tmp_path: Path) -> None:
    """A local model on a loopback endpoint does not egress (the common path)."""
    runtime = _make_runtime_with_model(
        FakeLLM(script=[]),
        FakeUI(),
        tmp_path,
        "gemma4:e4b",
        base_url="http://localhost:11434",
    )
    assert runtime._is_egressing() is False


# ---------------------------------------------------------------------------
# System-prompt honesty (v0.10.0 Part 2): the "no network" claim is conditional
# ---------------------------------------------------------------------------


def test_local_system_prompt_is_byte_identical(tmp_path: Path) -> None:
    """A non-egressing (local) session's system prompt is unchanged — zero regression."""
    from shellpilot.prompts.system import build_system_prompt

    runtime = make_runtime(FakeLLM(script=[]), FakeUI(), tmp_path)
    expected = build_system_prompt(
        workspace=tmp_path,
        profile="balanced",
    )
    assert runtime._context_snapshot().system_text().startswith(expected)
    assert "no independent network access" in runtime._system_message_text()
    assert "entirely on this machine" in runtime._system_message_text()


def test_egressing_system_prompt_drops_false_network_claim(tmp_path: Path) -> None:
    """An egressing session's prompt drops the false 'entirely on this machine' claim."""
    runtime = _make_runtime_with_model(
        FakeLLM(script=[]),
        FakeUI(),
        tmp_path,
        "nemotron-3-nano:30b-cloud",
        base_url="http://localhost:11434",
    )
    text = runtime._system_message_text()
    assert "no independent network access" not in text
    assert "entirely on this machine" not in text
    assert "leaves this device" in text


def test_build_system_prompt_egressing_flag() -> None:
    """build_system_prompt(is_egressing=True) replaces the local-only network line."""
    from shellpilot.prompts.system import build_system_prompt

    local = build_system_prompt(workspace=Path("/work"), profile="balanced")
    remote = build_system_prompt(workspace=Path("/work"), profile="balanced", is_egressing=True)
    assert "no independent network access" in local
    assert "no independent network access" not in remote
    assert "entirely on this machine" not in remote
    assert "leaves this device" in remote


# ---------------------------------------------------------------------------
# output_tokens accumulation across multi-call tool loop (thinking-stream plumbing)
# ---------------------------------------------------------------------------


def test_turn_stats_output_tokens_accumulated(tmp_path: Path) -> None:
    """TurnStats.output_tokens is the sum of output_tokens across all chat() calls in a turn.

    Uses two empty-content replies to trigger the empty-reply nudge path (the
    only multi-call-per-turn route that needs no tool registry or approvals).
    """
    import dataclasses

    from tests.fakes.fake_llm import answer

    first = dataclasses.replace(answer(""), output_tokens=100)  # empty → triggers nudge
    second = dataclasses.replace(answer("done"), output_tokens=50)  # real answer
    fake = FakeLLM(script=[first, second])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)
    runtime.run_turn("do something")
    assert len(ui.turn_stats) == 1
    assert ui.turn_stats[0].output_tokens == 150
