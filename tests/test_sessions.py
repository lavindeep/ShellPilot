"""Tests for session persistence, resume, and export (design section 25.2, v0.3.0)."""

from __future__ import annotations

from pathlib import Path

from shellpilot.config.loader import load_config
from shellpilot.llm.messages import Message, ToolCall
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.sessions import SessionStore, session_markdown
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI


def make_store(tmp_path: Path, session_id: str = "20260611-100000-ab12") -> SessionStore:
    return SessionStore(tmp_path / "sessions", session_id)


def test_round_trip_messages_and_meta(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_message(Message(role="user", content="hello"))
    store.record_message(
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(name="read_file", arguments={"path": "x.py"}),),
        )
    )
    store.record_message(Message(role="tool", content="contents of x"))

    loaded = SessionStore.load(store.path)
    assert loaded.session_id == "20260611-100000-ab12"
    assert loaded.model == "gemma4:e4b"
    assert [m.role for m in loaded.messages] == ["user", "assistant", "tool"]
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.messages[1].tool_calls[0].arguments == {"path": "x.py"}


def test_transcript_redacts_secrets(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_message(Message(role="tool", content="found AWS_KEY=AKIA1234567890ABCDEF in env"))
    raw = store.path.read_text(encoding="utf-8")
    assert "AKIA1234567890ABCDEF" not in raw


def test_clear_marker_resets_loaded_messages(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_message(Message(role="user", content="before clear"))
    store.record_clear()
    store.record_message(Message(role="user", content="after clear"))
    loaded = SessionStore.load(store.path)
    assert [m.content for m in loaded.messages] == ["after clear"]


def test_latest_picks_newest_session(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    make_store(tmp_path, "20260611-090000-aaaa").record_message(Message(role="user", content="old"))
    make_store(tmp_path, "20260611-110000-bbbb").record_message(Message(role="user", content="new"))
    latest = SessionStore.latest(directory)
    assert latest is not None and "110000-bbbb" in latest.name
    assert SessionStore.latest(tmp_path / "nope") is None
    assert SessionStore.find(directory, "20260611-090000-aaaa") is not None
    assert SessionStore.find(directory, "missing") is None


def make_runtime(
    tmp_path: Path, store: SessionStore | None, script: list[object]
) -> tuple[ConversationRuntime, FakeUI]:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    ui = FakeUI()
    runtime = ConversationRuntime(
        llm=FakeLLM(script=script),
        settings=loaded.settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        session=store,
    )
    return runtime, ui


def test_runtime_records_turns_incrementally(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    runtime, _ = make_runtime(tmp_path, store, [answer("hi there")])
    runtime.run_turn("hello")
    loaded = SessionStore.load(store.path)
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[1].content == "hi there"


def test_resume_restores_history_without_re_recording(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first, _ = make_runtime(tmp_path, store, [answer("first reply")])
    first.run_turn("first question")

    loaded = SessionStore.load(store.path)
    resumed, _ = make_runtime(tmp_path, store, [answer("second reply")])
    resumed.restore_history(loaded.messages)
    assert resumed.status().history_messages == 2
    assert len(resumed.snapshots) == 0  # stale snapshots never cross sessions

    resumed.run_turn("second question")
    final = SessionStore.load(store.path)
    # 2 original + 2 new; restore itself must not duplicate records.
    assert [m.role for m in final.messages] == ["user", "assistant", "user", "assistant"]


def test_clear_history_records_marker(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    runtime, _ = make_runtime(tmp_path, store, [answer("hi")])
    runtime.run_turn("hello")
    runtime.clear_history()
    assert SessionStore.load(store.path).messages == []


def test_session_markdown_renders_transcript(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_message(Message(role="user", content="fix the bug"))
    store.record_message(
        Message(
            role="assistant",
            content="On it.",
            tool_calls=(ToolCall(name="patch_file", arguments={"path": "x.py"}),),
        )
    )
    store.record_message(Message(role="tool", content="patched ok"))
    text = session_markdown(SessionStore.load(store.path))
    assert "# ShellPilot session" in text
    assert "fix the bug" in text
    assert "patch_file" in text
    assert "patched ok" in text
