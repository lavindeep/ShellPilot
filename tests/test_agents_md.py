"""Tests for AGENTS.md behavior-instruction loading (design section 16, v1 scope)."""

import hashlib
from pathlib import Path

from shellpilot.memory.agents_md import (
    load_behavior_instructions,
    project_agents_md_digest,
)


def test_missing_files_load_as_none(tmp_path: Path) -> None:
    instructions = load_behavior_instructions(
        config_dir=tmp_path / "config", workspace=tmp_path / "ws", max_tokens=1500
    )
    assert instructions.global_text is None
    assert instructions.project_text is None
    assert instructions.as_prompt_block() == ""


def test_loads_global_and_project(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "AGENTS.md").write_text("Always be concise.")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("This repo uses pytest.")

    instructions = load_behavior_instructions(
        config_dir=config_dir, workspace=workspace, max_tokens=1500
    )
    assert instructions.global_text == "Always be concise."
    assert instructions.project_text == "This repo uses pytest."
    block = instructions.as_prompt_block()
    assert "Always be concise." in block
    assert "This repo uses pytest." in block


def test_oversized_instructions_are_truncated(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "AGENTS.md").write_text("rule " * 5000)

    instructions = load_behavior_instructions(
        config_dir=config_dir, workspace=tmp_path / "ws", max_tokens=100
    )
    assert instructions.global_text is not None
    assert len(instructions.global_text) <= 100 * 4 + 50  # budget + truncation marker
    assert "truncated" in instructions.global_text


def test_project_digest_for_present_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    raw = "This repo uses pytest.\n"
    (workspace / "AGENTS.md").write_text(raw, encoding="utf-8")
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert project_agents_md_digest(workspace) == expected


def test_project_digest_none_for_absent_file(tmp_path: Path) -> None:
    assert project_agents_md_digest(tmp_path / "missing") is None


def test_project_digest_none_for_empty_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("   \n\t  ", encoding="utf-8")
    assert project_agents_md_digest(workspace) is None


def test_project_digest_changes_when_content_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text("Original instructions.", encoding="utf-8")
    first = project_agents_md_digest(workspace)
    agents.write_text("Malicious instructions.", encoding="utf-8")
    second = project_agents_md_digest(workspace)
    assert first is not None
    assert second is not None
    assert first != second


def test_untrusted_project_skips_project_text(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "AGENTS.md").write_text("Always be concise.")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("This repo uses pytest.")

    instructions = load_behavior_instructions(
        config_dir=config_dir,
        workspace=workspace,
        max_tokens=1500,
        project_trusted=False,
    )
    # Untrusted project AGENTS.md is not loaded; global remains.
    assert instructions.project_text is None
    assert instructions.global_text == "Always be concise."


def test_trusted_project_is_default(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("This repo uses pytest.")

    instructions = load_behavior_instructions(
        config_dir=tmp_path / "config", workspace=workspace, max_tokens=1500
    )
    assert instructions.project_text == "This repo uses pytest."
