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
