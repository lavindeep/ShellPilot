"""File snapshots for read-before-write safety (design section 12.4).

Write tools reject edits to files that were never read in this session or that
changed on disk after the read — no blind writes, no stale writes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def content_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    sha256: str


class SnapshotStore:
    """Session-scoped record of what the model has actually read."""

    def __init__(self) -> None:
        self._snapshots: dict[Path, FileSnapshot] = {}

    def record(self, path: Path, data: bytes) -> FileSnapshot:
        snapshot = FileSnapshot(path=path, sha256=content_hash(data))
        self._snapshots[path] = snapshot
        return snapshot

    def get(self, path: Path) -> FileSnapshot | None:
        return self._snapshots.get(path)

    def validate(self, path: Path) -> str | None:
        """None when the on-disk file matches the recorded snapshot, else why not."""
        snapshot = self._snapshots.get(path)
        if snapshot is None:
            return (
                f"{path.name} has not been read in this session; "
                "read it with read_file before writing"
            )
        try:
            current = path.read_bytes()
        except OSError as exc:
            return f"cannot re-read {path.name}: {exc}"
        if content_hash(current) != snapshot.sha256:
            return f"{path.name} changed on disk after it was read; read it again before writing"
        return None

    def forget(self, path: Path) -> None:
        self._snapshots.pop(path, None)

    def clear(self) -> None:
        """Drop all recorded snapshots (called by /clear to reset session state)."""
        self._snapshots.clear()

    def __len__(self) -> int:
        return len(self._snapshots)
