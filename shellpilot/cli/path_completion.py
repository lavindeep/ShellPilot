"""Filesystem path completion for slash-command arguments."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PathCompletionMatch:
    label: str
    fill: str


_PATH_COMMAND_PREFIXES = ("/cwd set ", "/attach ", "/export ")


def path_completion_matches(
    text: str, workspace: Path, *, limit: int = 20
) -> list[PathCompletionMatch]:
    parsed = _path_argument(text)
    if parsed is None:
        return []
    command_prefix, raw_path = parsed
    if "\n" in raw_path:
        return []

    search_dir, name_prefix = _search_parts(raw_path, workspace)
    try:
        entries = list(search_dir.iterdir())
    except OSError:
        return []

    matches: list[PathCompletionMatch] = []
    for entry in sorted(
        entries,
        key=lambda path: (not path.is_dir(), path.name.lower(), path.name),
    ):
        if not entry.name.startswith(name_prefix):
            continue
        if entry.name.startswith(".") and not name_prefix.startswith("."):
            continue
        suffix = "/" if entry.is_dir() else ""
        completed_label = _replace_leaf(_unescape_path(raw_path), entry.name + suffix)
        completed_fill = _replace_leaf(raw_path, _escape_path(entry.name) + suffix)
        matches.append(
            PathCompletionMatch(
                label=completed_label,
                fill=command_prefix + completed_fill,
            )
        )
        if len(matches) >= limit:
            break
    return matches


def _path_argument(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for prefix in _PATH_COMMAND_PREFIXES:
        if lowered.startswith(prefix):
            return text[: len(prefix)], text[len(prefix) :]
    return None


def _search_parts(raw_path: str, workspace: Path) -> tuple[Path, str]:
    lookup_path = _unescape_path(raw_path)
    if lookup_path == "" or lookup_path.endswith("/"):
        return _expand_path(lookup_path, workspace), ""
    path = _expand_path(lookup_path, workspace)
    return path.parent, path.name


def _expand_path(raw_path: str, workspace: Path) -> Path:
    if raw_path.startswith("~"):
        try:
            return Path(raw_path).expanduser()
        except RuntimeError:
            # Home cannot be resolved (no $HOME, no pwd entry) — this runs per
            # keystroke, so degrade to no matches instead of crashing the app.
            return workspace / raw_path
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return workspace / path


def _replace_leaf(raw_path: str, leaf: str) -> str:
    if raw_path == "" or raw_path.endswith("/"):
        return raw_path + leaf
    parent, separator, _name = raw_path.rpartition("/")
    if separator:
        return parent + separator + leaf
    return leaf


def _unescape_path(raw_path: str) -> str:
    if raw_path == "":
        return raw_path
    try:
        parts = shlex.split(raw_path)
    except ValueError:
        return raw_path
    return parts[0] if len(parts) == 1 else raw_path


def _escape_path(raw_path: str) -> str:
    return raw_path.replace("\\", "\\\\").replace(" ", "\\ ")
