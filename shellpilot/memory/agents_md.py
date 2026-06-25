"""Static behavior instructions from AGENTS.md files (design section 16, v1 scope).

V1 reads `<config-dir>/AGENTS.md` (global) and `<workspace>/AGENTS.md` (project)
at session start. Files are read-only-when-present: the assistant never creates
or writes them (settled open decision, design section 29).
"""

from __future__ import annotations

import hashlib
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


def project_agents_md_digest(workspace: Path) -> str | None:
    """SHA-256 of the raw bytes of ``<workspace>/AGENTS.md``, or None.

    Returns None when the file is absent, unreadable, or empty after stripping.
    Hashing the raw content means any change at all flips the digest, so a
    previously trusted project AGENTS.md must be re-accepted once it changes.
    """
    try:
        raw = (workspace / AGENTS_FILENAME).read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    return hashlib.sha256(raw).hexdigest()


def load_behavior_instructions(
    config_dir: Path,
    workspace: Path,
    max_tokens: int,
    *,
    project_trusted: bool = True,
) -> BehaviorInstructions:
    """Load global and project AGENTS.md, splitting the token budget between them.

    The global ``<config-dir>/AGENTS.md`` is always trusted. The project
    ``<workspace>/AGENTS.md`` is loaded only when *project_trusted* is True
    (trust-on-first-use, design section 16); when False it is skipped entirely.
    """
    per_file = max(1, max_tokens // 2)
    global_text = _read_bounded(config_dir / AGENTS_FILENAME, per_file)
    project_text = _read_bounded(workspace / AGENTS_FILENAME, per_file) if project_trusted else None
    return BehaviorInstructions(global_text=global_text, project_text=project_text)
