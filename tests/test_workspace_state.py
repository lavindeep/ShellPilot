"""Tests for workspace state persistence (design section 17)."""

from pathlib import Path

from shellpilot.persistence.workspace_state import (
    load_last_model,
    load_trusted_agents_digest,
    save_last_model,
    save_trusted_agents_digest,
    state_path,
)


def test_state_file_lives_under_dot_shellpilot(tmp_path: Path) -> None:
    assert state_path(tmp_path) == tmp_path / ".shellpilot" / "state.json"


def test_last_model_roundtrip(tmp_path: Path) -> None:
    save_last_model(tmp_path, "gemma4:e4b")
    assert load_last_model(tmp_path) == "gemma4:e4b"


def test_load_missing_state_returns_none(tmp_path: Path) -> None:
    assert load_last_model(tmp_path) is None


def test_load_corrupt_state_returns_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not json !!!")
    assert load_last_model(tmp_path) is None


def test_load_wrong_version_returns_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 99, "last_model": "gemma4:e4b"}\n', encoding="utf-8")
    assert load_last_model(tmp_path) is None


def test_load_missing_last_model_key_returns_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1}\n', encoding="utf-8")
    assert load_last_model(tmp_path) is None


def test_load_non_string_last_model_returns_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "last_model": 42}\n', encoding="utf-8")
    assert load_last_model(tmp_path) is None


def test_save_creates_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "new_workspace"
    workspace.mkdir()
    save_last_model(workspace, "gemma4:e2b")
    assert state_path(workspace).is_file()


def test_trusted_agents_digest_roundtrip(tmp_path: Path) -> None:
    save_trusted_agents_digest(tmp_path, "abc123")
    assert load_trusted_agents_digest(tmp_path) == "abc123"


def test_load_trusted_digest_missing_returns_none(tmp_path: Path) -> None:
    assert load_trusted_agents_digest(tmp_path) is None


def test_save_model_then_digest_keeps_both(tmp_path: Path) -> None:
    save_last_model(tmp_path, "gemma4:e4b")
    save_trusted_agents_digest(tmp_path, "deadbeef")
    assert load_last_model(tmp_path) == "gemma4:e4b"
    assert load_trusted_agents_digest(tmp_path) == "deadbeef"


def test_save_digest_then_model_keeps_both(tmp_path: Path) -> None:
    save_trusted_agents_digest(tmp_path, "deadbeef")
    save_last_model(tmp_path, "gemma4:e4b")
    assert load_trusted_agents_digest(tmp_path) == "deadbeef"
    assert load_last_model(tmp_path) == "gemma4:e4b"


def test_old_state_without_digest_key(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "last_model": "gemma4:e4b"}\n', encoding="utf-8")
    assert load_trusted_agents_digest(tmp_path) is None
    assert load_last_model(tmp_path) == "gemma4:e4b"


def test_load_non_string_digest_returns_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "trusted_agents_md": 42}\n', encoding="utf-8")
    assert load_trusted_agents_digest(tmp_path) is None
