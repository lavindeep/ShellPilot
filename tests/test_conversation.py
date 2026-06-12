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


def test_empty_first_reply_is_not_nudged(tmp_path: Path) -> None:
    """An empty FIRST reply (no tool ran yet) ends the turn without nudging."""
    fake = FakeLLM(script=[answer("")])
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("hello")

    assert reply == ""
    assert len(fake.calls) == 1
    assert not any("(empty response)" in status for status in ui.statuses)


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
