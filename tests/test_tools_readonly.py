"""Tests for the read-only tools in real temp directories."""

from pathlib import Path

import pytest

from shellpilot.tools.base import ToolContext, WorkspaceBoundaryError, resolve_in_workspace
from shellpilot.tools.environment import ENV_INFO
from shellpilot.tools.filesystem import LIST_DIR, READ_FILE
from shellpilot.tools.search import SEARCH_TEXT


def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, max_result_tokens=2000)


# -- read_file ----------------------------------------------------------------


def test_read_file_happy(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\nprint('there')\n")
    result = READ_FILE.handler(ctx(tmp_path), {"path": "hello.py"})
    assert result.success
    assert "print('hi')" in result.content
    assert "lines 1-2 of 2" in result.summary


def test_read_file_window(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join(f"line{i}" for i in range(1, 401)))
    result = READ_FILE.handler(
        ctx(tmp_path), {"path": "big.txt", "start_line": 100, "max_lines": 3}
    )
    assert result.success
    assert result.content.splitlines() == ["line100", "line101", "line102"]
    assert result.truncated  # window smaller than file


def test_read_file_missing(tmp_path: Path) -> None:
    result = READ_FILE.handler(ctx(tmp_path), {"path": "nope.txt"})
    assert not result.success


def test_read_file_refuses_binary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02data")
    result = READ_FILE.handler(ctx(tmp_path), {"path": "blob.bin"})
    assert not result.success
    assert "binary" in result.summary


def test_read_file_outside_workspace_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    with pytest.raises(WorkspaceBoundaryError):
        READ_FILE.handler(ctx(workspace), {"path": "../secret.txt"})


def test_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "alias.txt").symlink_to(outside)
    with pytest.raises(WorkspaceBoundaryError):
        resolve_in_workspace(workspace, "alias.txt")


# -- list_dir -----------------------------------------------------------------


def test_list_dir_happy(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "a.py").write_text("x = 1")
    result = LIST_DIR.handler(ctx(tmp_path), {"path": "."})
    assert result.success
    assert "pkg/" in result.content
    assert "a.py" in result.content


def test_list_dir_on_file_fails(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    result = LIST_DIR.handler(ctx(tmp_path), {"path": "a.py"})
    assert not result.success


# -- search_text --------------------------------------------------------------


def test_search_finds_matches_and_skips_git(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def load_config():\n    pass\n")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "junk.txt").write_text("load_config here too")

    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "load_config"})
    assert result.success
    assert "src/app.py:1" in result.content
    assert ".git" not in result.content
    assert "1 matches" in result.summary


def test_search_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00load_config\x00")
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "load_config"})
    assert result.success
    assert result.content == ""


def test_search_empty_pattern_fails(tmp_path: Path) -> None:
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": ""})
    assert not result.success


def test_search_bounds_matches(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("needle\n" * 500)
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "needle"})
    assert result.truncated
    assert "100 matches" in result.summary


# -- env_info -----------------------------------------------------------------


def test_env_info_reports_environment(tmp_path: Path) -> None:
    result = ENV_INFO.handler(ctx(tmp_path), {})
    assert result.success
    assert "os:" in result.content
    assert "python:" in result.content
    assert str(tmp_path) in result.content
