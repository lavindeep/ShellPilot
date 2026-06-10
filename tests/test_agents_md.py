"""Tests for AGENTS.md behavior-instruction loading (design section 16, v1 scope)."""

from pathlib import Path

from shellpilot.memory.agents_md import load_behavior_instructions


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
