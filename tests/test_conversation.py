"""Tests for the unified conversation runtime with the fake model."""

from pathlib import Path

from shellpilot.config.model import ContextSettings, Settings
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
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
