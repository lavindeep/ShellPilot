"""view_image tool: next-message image delivery (task B10).

The tool stages an ImageRef; the runtime drains the stage after the tool-call
batch and appends a synthetic user-role message carrying the image with a
harness marker, because Ollama vision chat templates only render images on
user messages.
"""

from __future__ import annotations

import json
from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.llm.messages import ImageRef
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.sessions import SessionStore
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.tools.images import make_view_image_tool
from tests.conftest import TINY_PNG
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI

HARNESS_MARKER = "[harness: image attached"


def make_runtime(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    *,
    session: SessionStore | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        session=session,
    )


def write_png(tmp_path: Path, name: str = "pic.png") -> Path:
    target = tmp_path / name
    target.write_bytes(TINY_PNG)
    return target


def test_view_image_stages_image_for_next_model_call(tmp_path: Path) -> None:
    write_png(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="pic.png"),
            answer("a red pixel"),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("look at pic.png")

    assert reply == "a red pixel"
    # The SECOND chat call (after the tool batch) must carry the synthetic
    # user-role message with exactly one image and the harness marker.
    second = fake.calls[1]
    image_users = [m for m in second.messages if m.role == "user" and HARNESS_MARKER in m.content]
    assert len(image_users) == 1
    assert len(image_users[0].images) == 1
    assert "pic.png" in image_users[0].content


def test_view_image_rejects_non_vision_model(tmp_path: Path) -> None:
    write_png(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="pic.png"),
            answer("cannot see images"),
        ],
        capabilities=("completion", "tools"),  # no vision
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("look at pic.png")

    tool_messages = [m for m in runtime._history if m.role == "tool"]
    assert any("/model use" in m.content for m in tool_messages)
    # NO synthetic image-bearing user message recorded.
    assert not any(m.role == "user" and HARNESS_MARKER in m.content for m in runtime._history)
    assert all(not m.images for m in runtime._history if m.role == "user")


def test_view_image_enforces_workspace_boundary(tmp_path: Path) -> None:
    write_png(tmp_path)
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="../outside.png"),
            answer("blocked"),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("look outside")

    tool_messages = [m for m in runtime._history if m.role == "tool"]
    assert any("workspace boundary" in m.content for m in tool_messages)
    # Nothing staged or attached.
    assert not any(m.role == "user" and HARNESS_MARKER in m.content for m in runtime._history)


def test_view_image_rejects_bad_file(tmp_path: Path) -> None:
    # No file written; path is missing.
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="missing.png"),
            answer("no such file"),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("look at missing.png")

    tool_messages = [m for m in runtime._history if m.role == "tool"]
    assert any("not a file" in m.content for m in tool_messages)
    assert not any(m.role == "user" and HARNESS_MARKER in m.content for m in runtime._history)


def test_staged_images_cleared_between_turns(tmp_path: Path) -> None:
    write_png(tmp_path)
    # Turn 1 stages an image and ends after the answer.
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="pic.png"),
            answer("a red pixel"),
            # Turn 2: a plain answer, no view_image.
            answer("hello again"),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    runtime.run_turn("look at pic.png")
    calls_after_turn1 = len(fake.calls)
    runtime.run_turn("say hi")

    # The second turn's chat calls must NOT contain any NEW image-bearing
    # synthetic user message beyond the one already created in turn 1.
    turn2_calls = fake.calls[calls_after_turn1:]
    for call in turn2_calls:
        new_markers = [m for m in call.messages if m.role == "user" and HARNESS_MARKER in m.content]
        # At most the single marker from turn 1 may persist in history; turn 2
        # must not have produced a second one.
        assert len(new_markers) <= 1
    # Exactly one harness marker exists in the whole history.
    markers = [m for m in runtime._history if m.role == "user" and HARNESS_MARKER in m.content]
    assert len(markers) == 1


def test_view_image_transcript_reference_only(tmp_path: Path) -> None:
    write_png(tmp_path)
    session = SessionStore(tmp_path / "sessions", "20260611-100000-ab12")
    fake = FakeLLM(
        script=[
            tool_call("view_image", path="pic.png"),
            answer("a red pixel"),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path, session=session)

    runtime.run_turn("look at pic.png")

    raw = session.path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    image_records = [r for r in records if r.get("images")]
    assert len(image_records) == 1
    entry = image_records[0]["images"][0]
    assert "path" in entry and "sha256" in entry
    assert "data_b64" not in entry
    # The base64 payload of the staged image must never appear in the transcript.
    import base64

    b64 = base64.b64encode(TINY_PNG).decode()
    assert b64 not in raw


def test_view_image_listed_for_both_profiles() -> None:
    spec = make_view_image_tool(lambda ref: None, lambda: True)
    assert spec.name == "view_image"
    assert spec.side_effect is SideEffect.NONE
    assert spec.default_risk is RiskLevel.LOW
    assert spec.allowed_profiles == frozenset({"supervised", "balanced"})


def test_view_image_handler_stages_on_success(tmp_path: Path) -> None:
    """Unit-level: a successful view_image call invokes stage exactly once."""
    from shellpilot.tools.base import ToolContext

    write_png(tmp_path)
    staged: list[ImageRef] = []
    spec = make_view_image_tool(staged.append, lambda: True)
    context = ToolContext(workspace=tmp_path, max_result_tokens=2000)

    result = spec.handler(context, {"path": "pic.png"})

    assert result.success
    assert result.summary == "viewed pic.png"
    assert len(staged) == 1
    assert staged[0].path.endswith("pic.png")
