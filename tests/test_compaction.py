"""Tests for selective token-budget compaction (design section 20.2, v0.3.0)."""

from __future__ import annotations

from pathlib import Path

from shellpilot.config.model import ContextSettings, RuntimeSettings, Settings
from shellpilot.llm.messages import Message, ToolCall
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI


def make_runtime(
    tmp_path: Path,
    *,
    context_tokens: int = 512,
    auto_compact: bool = True,
    script: list[object] | None = None,
) -> tuple[ConversationRuntime, FakeUI, FakeLLM]:
    settings = Settings(
        context=ContextSettings(model_context_tokens=context_tokens),
        runtime=RuntimeSettings(auto_compact=auto_compact),
    )
    fake = FakeLLM(script=script or [])
    ui = FakeUI()
    runtime = ConversationRuntime(
        llm=fake,
        settings=settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )
    return runtime, ui, fake


def test_old_tool_results_are_digested_before_anything_drops(tmp_path: Path) -> None:
    runtime, _, _ = make_runtime(tmp_path, context_tokens=2048)
    big_output = "line of tool output\n" * 300
    runtime._history.extend(
        [
            Message(role="user", content="please inspect the project"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(name="read_file", arguments={"path": "x"}),),
            ),
            Message(role="tool", content=big_output),
            Message(role="assistant", content="summary of findings"),
            Message(role="user", content="now fix it"),
            Message(role="assistant", content="working on it"),
        ]
    )
    runtime.compact_now()
    roles = [message.role for message in runtime._history]
    assert roles.count("user") == 2  # no user message was dropped
    tool_messages = [m for m in runtime._history if m.role == "tool"]
    assert tool_messages, "tool result should be digested, not dropped"
    assert "compacted" in tool_messages[0].content
    assert len(tool_messages[0].content) < len(big_output)


def test_assistant_tool_call_drops_together_with_results(tmp_path: Path) -> None:
    runtime, _, _ = make_runtime(tmp_path, context_tokens=256)
    runtime._history.extend(
        [
            Message(role="user", content="inspect " + "pad " * 40),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(name="read_file", arguments={"path": "x"}),),
            ),
            Message(role="tool", content="output " * 60),
            Message(role="assistant", content="findings " + "pad " * 40),
            Message(role="user", content="thanks"),
            Message(role="assistant", content="done"),
            Message(role="user", content="one more thing"),
            Message(role="assistant", content="sure"),
        ]
    )
    runtime.compact_now()
    roles = [message.role for message in runtime._history]
    # No orphaned tool message may remain after its assistant parent dropped.
    for index, message in enumerate(runtime._history):
        if message.role == "tool":
            parent = runtime._history[index - 1]
            assert parent.role == "assistant" and parent.tool_calls
    assert "user" in roles  # user messages survive longest


def test_user_messages_drop_last_and_newest_is_kept(tmp_path: Path) -> None:
    runtime, _, _ = make_runtime(tmp_path, context_tokens=96)
    for index in range(6):
        runtime._history.append(Message(role="user", content=f"instruction {index} " + "pad " * 30))
    runtime.compact_now()
    user_contents = [m.content for m in runtime._history if m.role == "user"]
    assert user_contents, "the most recent user message must survive"
    assert any("instruction 5" in content for content in user_contents)


def test_auto_compact_off_refuses_past_hard_limit(tmp_path: Path) -> None:
    runtime, ui, fake = make_runtime(
        tmp_path, context_tokens=256, auto_compact=False, script=[answer("hi")]
    )
    runtime._history.extend(Message(role="user", content="pad " * 100) for _ in range(4))
    reply = runtime.run_turn("over the limit now")
    assert reply == ""
    assert fake.calls == []  # the model was never called
    assert any("hard limit" in status.lower() for status in ui.statuses)


def test_auto_compact_off_allows_normal_turns(tmp_path: Path) -> None:
    runtime, _, fake = make_runtime(
        tmp_path, context_tokens=4096, auto_compact=False, script=[answer("hi")]
    )
    assert runtime.run_turn("hello") == "hi"
    assert len(fake.calls) == 1


def test_auto_compact_off_counts_incoming_image_tokens(tmp_path: Path) -> None:
    """The pre-turn hard-limit gate must count THIS turn's incoming images, not
    only text and history images (design section 10.5)."""
    import base64
    import hashlib

    from shellpilot.llm.messages import ImageRef
    from shellpilot.runtime.conversation import IMAGE_TOKEN_ESTIMATE
    from tests.conftest import TINY_PNG

    ref = ImageRef(
        path="/tmp/img.png",
        sha256=hashlib.sha256(TINY_PNG).hexdigest(),
        data_b64=base64.b64encode(TINY_PNG).decode(),
        size_bytes=len(TINY_PNG),
    )
    runtime, ui, fake = make_runtime(
        tmp_path, context_tokens=4096, auto_compact=False, script=[answer("seen")]
    )
    # An empty-text, no-image turn is comfortably under the hard limit; carrying
    # enough images to close that gap must trip the gate (text stays empty).
    headroom = runtime.budget.hard_limit_tokens - runtime.estimated_prompt_tokens()
    assert headroom > 0
    image_count = headroom // IMAGE_TOKEN_ESTIMATE + 1
    reply = runtime.run_turn("", images=tuple(ref for _ in range(image_count)))
    assert reply == ""
    assert fake.calls == []  # the gate fired before any model call
    assert any("hard limit" in status.lower() for status in ui.statuses)


def test_auto_compact_off_text_near_limit_unaffected(tmp_path: Path) -> None:
    """A text-only turn under the hard limit is allowed exactly as before."""
    runtime, _, fake = make_runtime(
        tmp_path, context_tokens=4096, auto_compact=False, script=[answer("seen")]
    )
    assert runtime.estimated_prompt_tokens() < runtime.budget.hard_limit_tokens
    assert runtime.run_turn("hello") == "seen"
    assert len(fake.calls) == 1
