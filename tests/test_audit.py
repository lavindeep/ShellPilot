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
