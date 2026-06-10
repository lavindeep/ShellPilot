"""Static behavior instructions from AGENTS.md files (design section 16, v1 scope).

V1 reads `<config-dir>/AGENTS.md` (global) and `<workspace>/AGENTS.md` (project)
at session start. Files are read-only-when-present: the assistant never creates
or writes them (settled open decision, design section 29).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shellpilot.runtime.budget import truncate_to_tokens

AGENTS_FILENAME = "AGENTS.md"


@dataclass(frozen=True)
class BehaviorInstructions:
    """Loaded behavior instructions, already bounded to the prompt budget."""

    global_text: str | None
    project_text: str | None

    def as_prompt_block(self) -> str:
        parts: list[str] = []
        if self.global_text:
            parts.append(f"## User behavior instructions (global)\n{self.global_text}")
        if self.project_text:
            parts.append(f"## Project instructions\n{self.project_text}")
        return "\n\n".join(parts)


def _read_bounded(path: Path, max_tokens: int) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    bounded, _ = truncate_to_tokens(text, max_tokens)
    return bounded


def load_behavior_instructions(
    config_dir: Path, workspace: Path, max_tokens: int
) -> BehaviorInstructions:
    """Load global and project AGENTS.md, splitting the token budget between them."""
    per_file = max(1, max_tokens // 2)
    global_text = _read_bounded(config_dir / AGENTS_FILENAME, per_file)
    project_text = _read_bounded(workspace / AGENTS_FILENAME, per_file)
    return BehaviorInstructions(global_text=global_text, project_text=project_text)
