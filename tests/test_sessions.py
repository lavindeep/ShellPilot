"""Tests for session persistence, resume, and export (design section 25.2, v0.3.0)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from shellpilot.config.loader import load_config
from shellpilot.llm.messages import ImageRef, Message, ToolCall
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.sessions import SessionStore, session_markdown
from shellpilot.runtime.conversation import ConversationRuntime
from tests.conftest import TINY_PNG
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


# ---------------------------------------------------------------------------
# B8: transcript image refs — path+hash only, never bytes
# ---------------------------------------------------------------------------


def _make_image_ref(data: bytes, path: str = "/tmp/shot.png") -> ImageRef:
    sha256 = hashlib.sha256(data).hexdigest()
    data_b64 = base64.b64encode(data).decode()
    return ImageRef(path=path, sha256=sha256, data_b64=data_b64)


def test_record_message_stores_image_refs_not_bytes(tmp_path: Path) -> None:
    """record_message serialises path and sha256 but NEVER the base64 payload."""
    ref = _make_image_ref(TINY_PNG, path="/home/user/screenshot.png")
    msg = Message(role="user", content="look", images=(ref,))
    store = make_store(tmp_path)
    store.record_message(msg)

    raw = store.path.read_text(encoding="utf-8")
    record = json.loads(raw.strip())

    # Path and sha256 must be present.
    assert record["images"] == [{"path": ref.path, "sha256": ref.sha256}]
    # The base64 payload must never appear in the raw line.
    assert ref.data_b64 not in raw


def test_load_drops_images(tmp_path: Path) -> None:
    """Round-trip: loaded messages have images=() (visual context not restored on resume)."""
    ref = _make_image_ref(TINY_PNG)
    msg = Message(role="user", content="see this", images=(ref,))
    store = make_store(tmp_path)
    store.record_message(msg)

    loaded = SessionStore.load(store.path)
    assert len(loaded.messages) == 1
    assert loaded.messages[0].images == ()
    assert loaded.messages[0].content == "see this"


def test_export_notes_attached_images(tmp_path: Path) -> None:
    """session_markdown lists each image as '- image: <path> (sha256 <8chars>…)'."""
    ref = _make_image_ref(TINY_PNG, path="/home/user/diagram.png")
    msg = Message(role="user", content="explain this diagram", images=(ref,))
    store = make_store(tmp_path)
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_message(msg)
    store.record_message(Message(role="assistant", content="Sure."))

    # Load drops images, so we test markdown on the live message directly.
    from shellpilot.persistence.sessions import LoadedSession

    session = LoadedSession(
        session_id="test",
        model="gemma4:e4b",
        profile="balanced",
        messages=[msg, Message(role="assistant", content="Sure.")],
    )
    text = session_markdown(session)
    assert "image:" in text
    assert ref.path in text
    assert ref.sha256[:8] in text


# ---------------------------------------------------------------------------
# P1 fix: tool-call argument redaction in session transcripts and /export
# ---------------------------------------------------------------------------

_SECRET_ARG = "api_key=sk-supersecret123456"  # matches the key=value pattern in redaction.py


def test_tool_call_arguments_redacted_in_jsonl(tmp_path: Path) -> None:
    """Assistant tool-call arguments containing secrets are redacted in the JSONL transcript."""
    store = make_store(tmp_path)
    store.record_message(
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(name="write_file", arguments={"content": _SECRET_ARG}),),
        )
    )
    raw = store.path.read_text(encoding="utf-8")
    assert "sk-supersecret123456" not in raw
    assert "[REDACTED]" in raw


def test_tool_call_arguments_redacted_in_export(tmp_path: Path) -> None:
    """/export (session_markdown) re-reads the JSONL, so it inherits redaction from disk."""
    store = make_store(tmp_path)
    store.write_meta(model="gemma4:e4b", profile="balanced", workspace=tmp_path)
    store.record_message(
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(name="write_file", arguments={"content": _SECRET_ARG}),),
        )
    )
    text = session_markdown(SessionStore.load(store.path))
    assert "sk-supersecret123456" not in text
    assert "[REDACTED]" in text


def test_tool_call_arguments_raw_when_redaction_disabled(tmp_path: Path) -> None:
    """When redact=False the arguments are persisted verbatim."""
    store = SessionStore(tmp_path / "sessions", "test-nored", redact=False)
    store.record_message(
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(name="write_file", arguments={"content": _SECRET_ARG}),),
        )
    )
    raw = store.path.read_text(encoding="utf-8")
    assert "sk-supersecret123456" in raw


def test_tool_result_content_redaction_unchanged(tmp_path: Path) -> None:
    """Tool-result (role=tool) content is still redacted as before."""
    store = make_store(tmp_path)
    store.record_message(Message(role="tool", content=f"output: {_SECRET_ARG}"))
    raw = store.path.read_text(encoding="utf-8")
    assert "sk-supersecret123456" not in raw
    assert "[REDACTED]" in raw


def test_tool_call_non_string_nested_argument_roundtrip(tmp_path: Path) -> None:
    """Non-string nested arguments (list/dict/int) pass through without crashing."""
    args: dict[str, object] = {
        "paths": ["/a", "/b"],
        "flags": {"verbose": True, "depth": 3},
        "count": 42,
    }
    store = make_store(tmp_path)
    store.record_message(
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(name="list_dir", arguments=args),),
        )
    )
    loaded = SessionStore.load(store.path)
    assert loaded.messages[0].tool_calls[0].arguments == args


# ---------------------------------------------------------------------------
# Fix 1: corrupt-line guard in SessionStore.load
# ---------------------------------------------------------------------------


def test_load_skips_truncated_final_line(tmp_path: Path) -> None:
    """A truncated (corrupt) final line is silently skipped; valid messages load."""
    store = make_store(tmp_path)
    store.record_message(Message(role="user", content="hello"))
    store.record_message(Message(role="assistant", content="world"))
    # Append a truncated JSON line (simulates crash mid-write)
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "message", "role": "user", "content": "trun')
    loaded = SessionStore.load(store.path)
    assert [m.content for m in loaded.messages] == ["hello", "world"]


def test_load_skips_corrupt_middle_line(tmp_path: Path) -> None:
    """A corrupt line in the middle is skipped; later valid lines still load."""
    store = make_store(tmp_path)
    store.record_message(Message(role="user", content="before"))
    # Inject a corrupt line manually between the two valid records.
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("not-valid-json\n")
    store.record_message(Message(role="assistant", content="after"))
    loaded = SessionStore.load(store.path)
    assert [m.content for m in loaded.messages] == ["before", "after"]
