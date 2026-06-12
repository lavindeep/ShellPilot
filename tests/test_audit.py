"""Tests for the JSONL audit log (design section 22)."""

import json
from pathlib import Path

from shellpilot.persistence.audit_store import AuditLogger


def make_logger(tmp_path: Path, redact: bool = True) -> AuditLogger:
    return AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="abc123",
        workspace=tmp_path,
        profile="balanced",
        redact=redact,
    )


def test_events_are_json_lines_with_required_fields(tmp_path: Path) -> None:
    logger = make_logger(tmp_path)
    logger.write("command_approval", command="rm -rf build/", risk="high", decision="approved")
    logger.write("session_end")

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    event = json.loads(lines[0])
    assert event["version"] == 1
    assert event["session_id"] == "abc123"
    assert event["event"] == "command_approval"
    assert event["risk"] == "high"
    assert event["decision"] == "approved"
    assert event["profile"] == "balanced"
    assert "timestamp" in event


def test_secrets_redacted_in_events(tmp_path: Path) -> None:
    logger = make_logger(tmp_path)
    logger.write("command_result", command="export TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345")
    event = json.loads((tmp_path / "audit.jsonl").read_text())
    assert "ghp_" not in event["command"]


def test_redaction_can_be_disabled(tmp_path: Path) -> None:
    logger = make_logger(tmp_path, redact=False)
    logger.write("command_result", command="AKIAIOSFODNN7EXAMPLE")
    event = json.loads((tmp_path / "audit.jsonl").read_text())
    assert event["command"] == "AKIAIOSFODNN7EXAMPLE"


def test_tail_returns_recent_events(tmp_path: Path) -> None:
    logger = make_logger(tmp_path)
    for index in range(30):
        logger.write("user_turn", chars=index)
    events = logger.tail(10)
    assert len(events) == 10
    assert events[-1]["chars"] == 29


def test_tail_on_missing_file(tmp_path: Path) -> None:
    assert make_logger(tmp_path).tail() == []


# ---------------------------------------------------------------------------
# Fix 3: /logs session filtering
# ---------------------------------------------------------------------------


def test_tail_session_filter_scans_whole_file(tmp_path: Path) -> None:
    """tail(session_id=...) must find events even when buried before 200+ later lines.

    The failure mode of the old code: the target session wrote events early,
    then 250+ events from other sessions pushed them out of the last-200-line
    scan window, making tail(15, session_id=...) return nothing.
    """
    audit_path = tmp_path / "audit.jsonl"
    # Write 15 events for the target session first (they end up near the top).
    target_logger = AuditLogger(
        path=audit_path,
        session_id="target-session",
        workspace=tmp_path,
        profile="balanced",
    )
    for i in range(15):
        target_logger.write("user_turn", chars=i)

    # Write 250 events for other sessions — this pushes the target session
    # entirely out of the old max(15*10, 200) = 200-line scan window.
    noise_logger = AuditLogger(
        path=audit_path,
        session_id="noise-session",
        workspace=tmp_path,
        profile="balanced",
    )
    for i in range(250):
        noise_logger.write("user_turn", chars=i)

    # The target session's 15 events are at lines 0-14 of a 265-line file;
    # the old code only scanned the last 200 lines (lines 65-264) and missed them.
    events = target_logger.tail(15, session_id="target-session")
    assert len(events) == 15
    assert all(e["session_id"] == "target-session" for e in events)


def test_tail_global_without_session_id_unchanged(tmp_path: Path) -> None:
    """Global tail (no session_id) continues to return the last N lines regardless."""
    logger = make_logger(tmp_path)
    for i in range(50):
        logger.write("user_turn", chars=i)
    events = logger.tail(10)
    assert len(events) == 10
    assert events[-1]["chars"] == 49


def test_tail_session_filter_returns_only_matching_events(tmp_path: Path) -> None:
    """tail(session_id=...) returns only events for that session."""
    logger_a = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="session-aaa",
        workspace=tmp_path,
        profile="balanced",
    )
    logger_b = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="session-bbb",
        workspace=tmp_path,
        profile="balanced",
    )
    logger_a.write("user_turn", chars=1)
    logger_b.write("user_turn", chars=2)
    logger_a.write("session_end")

    events_a = logger_a.tail(20, session_id="session-aaa")
    events_b = logger_a.tail(20, session_id="session-bbb")
    events_all = logger_a.tail(20)

    assert all(e["session_id"] == "session-aaa" for e in events_a)
    assert len(events_a) == 2
    assert all(e["session_id"] == "session-bbb" for e in events_b)
    assert len(events_b) == 1
    assert len(events_all) == 3


# ---------------------------------------------------------------------------
# Fix 2: audit events after /cwd carry the new workspace path
# ---------------------------------------------------------------------------


def test_audit_events_carry_updated_workspace_after_set_workspace(tmp_path: Path) -> None:
    """After ConversationRuntime.set_workspace the audit logger's workspace field is
    updated so subsequent events carry the new path, not the original one."""
    from shellpilot.config.loader import load_config
    from shellpilot.memory.agents_md import BehaviorInstructions
    from shellpilot.runtime.conversation import ConversationRuntime
    from tests.fakes.fake_llm import FakeLLM
    from tests.fakes.fake_ui import FakeUI

    logger = make_logger(tmp_path)
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    runtime = ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=loaded.settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        audit=logger,
    )

    new_ws = tmp_path / "new_ws"
    new_ws.mkdir()
    runtime.set_workspace(new_ws)
    # Write a subsequent event after the workspace change.
    logger.write("user_turn", chars=5)

    events = logger.tail(20)
    # The config_change event itself must carry the new workspace.
    change_event = next(e for e in events if e.get("event") == "config_change")
    assert change_event["workspace"] == str(new_ws)
    # The subsequent user_turn event must also carry the new workspace.
    turn_event = next(e for e in events if e.get("event") == "user_turn")
    assert turn_event["workspace"] == str(new_ws)
