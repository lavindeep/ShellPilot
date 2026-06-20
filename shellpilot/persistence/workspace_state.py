"""Workspace harness state persisted in .shellpilot/state.json (design section 17).

This file stores harness-internal state for a workspace — not user config.
It records the last model selected by the user (so the boot picker can
pre-select it on the next launch) and the digest of the project AGENTS.md the
user has accepted (trust-on-first-use, design section 16). Saving one key
must never clobber the other, so all writers read-merge-write the whole state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shellpilot.persistence.json_store import atomic_write_json
from shellpilot.persistence.paths import project_state_dir

STATE_VERSION = 1


def state_path(workspace: Path) -> Path:
    """Absolute path to the workspace harness state file."""
    return project_state_dir(workspace) / "state.json"


def _load_state(workspace: Path) -> dict[str, Any]:
    """Return the parsed state dict, or ``{}`` on any problem.

    Returns ``{}`` (never raises) when the file is absent, unreadable,
    contains invalid JSON, or carries an unexpected version.
    """
    path = state_path(workspace)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != STATE_VERSION:
        return {}
    return data


def _save_state(workspace: Path, data: dict[str, Any]) -> None:
    """Atomically persist *data* (stamping the current version).

    Creates ``.shellpilot/`` if it does not exist yet. Writes atomically so a
    crash mid-write leaves the previous state intact.
    """
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "version": STATE_VERSION}
    atomic_write_json(path, data)


def load_last_model(workspace: Path) -> str | None:
    """Return the last model name stored for *workspace*, or None on any problem.

    Returns None (never raises) when the state is absent/invalid or is missing
    a string *last_model* key.
    """
    model = _load_state(workspace).get("last_model")
    return model if isinstance(model, str) else None


def save_last_model(workspace: Path, model: str) -> None:
    """Persist *model* as the last-selected model for *workspace*.

    Merges into existing state so the trusted-AGENTS.md digest is preserved.
    """
    state = _load_state(workspace)
    state["last_model"] = model
    _save_state(workspace, state)


def load_trusted_agents_digest(workspace: Path) -> str | None:
    """Return the accepted project-AGENTS.md digest for *workspace*, or None.

    Returns None when the state is absent/invalid or the key is missing or
    not a string.
    """
    digest = _load_state(workspace).get("trusted_agents_md")
    return digest if isinstance(digest, str) else None


def save_trusted_agents_digest(workspace: Path, digest: str) -> None:
    """Persist *digest* as the accepted project-AGENTS.md fingerprint.

    Merges into existing state so the last-selected model is preserved.
    """
    state = _load_state(workspace)
    state["trusted_agents_md"] = digest
    _save_state(workspace, state)
