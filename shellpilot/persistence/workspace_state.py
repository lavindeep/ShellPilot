"""Workspace harness state persisted in .shellpilot/state.json (design section 17).

This file stores harness-internal state for a workspace — not user config.
Currently it records the last model selected by the user so that the boot
picker (Task A8) can pre-select it on the next launch.
"""

from __future__ import annotations

import json
from pathlib import Path

from shellpilot.persistence.json_store import atomic_write_json
from shellpilot.persistence.paths import project_state_dir

STATE_VERSION = 1


def state_path(workspace: Path) -> Path:
    """Absolute path to the workspace harness state file."""
    return project_state_dir(workspace) / "state.json"


def load_last_model(workspace: Path) -> str | None:
    """Return the last model name stored for *workspace*, or None on any problem.

    Returns None (never raises) when the file is absent, unreadable,
    contains invalid JSON, carries an unexpected version, or is missing
    a string *last_model* key.
    """
    path = state_path(workspace)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != STATE_VERSION:
        return None
    model = data.get("last_model")
    if not isinstance(model, str):
        return None
    return model


def save_last_model(workspace: Path, model: str) -> None:
    """Persist *model* as the last-selected model for *workspace*.

    Creates ``.shellpilot/`` if it does not exist yet.
    Writes atomically so a crash mid-write leaves the previous state intact.
    """
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"version": STATE_VERSION, "last_model": model})
