"""Persistent runtime config-overrides layer (design section 17).

The overrides file is a runtime-managed JSON sidecar next to the user
config.toml.  Errors in this file NEVER raise — invalid entries are
dropped with collected warnings so the program always boots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shellpilot.persistence.json_store import atomic_write_json


def overrides_path(config_dir: Path) -> Path:
    """Return the canonical path for the overrides sidecar."""
    return config_dir / "overrides.json"


def load_overrides(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Load the overrides file, self-healing on any error.

    Returns ``(values, warnings)`` where *values* is the parsed dict and
    *warnings* is a list of human-readable strings describing problems
    that caused entries (or the entire file) to be ignored.

    Contract:
    - Missing file  → ``({}, [])``.
    - Unreadable / corrupt JSON, or top-level value is not a dict  →
      ``({}, [<one file-level warning>])``.  The bad file is left on
      disk; the program still boots.
    """
    if not path.exists():
        return {}, []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [
            f"overrides file {path} could not be read ({exc}); "
            "it was ignored — fix it manually or run /config reset"
        ]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [
            f"overrides file {path} contains invalid JSON ({exc}); "
            "it was ignored — fix it manually or run /config reset"
        ]

    if not isinstance(data, dict):
        return {}, [
            f"overrides file {path} must be a JSON object, got "
            f"{type(data).__name__}; "
            "it was ignored — fix it manually or run /config reset"
        ]

    return data, []


def save_overrides(path: Path, values: dict[str, Any]) -> None:
    """Atomically write *values* to *path* as a JSON object."""
    atomic_write_json(path, values)
