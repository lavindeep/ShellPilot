"""Structured local audit log (design section 22): JSONL, redacted, local-only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shellpilot.memory.redaction import redact_structure

AUDIT_VERSION = 1


def _redact_value(value: Any, enabled: bool) -> Any:
    if not enabled:
        return value
    return redact_structure(value)


@dataclass
class AuditLogger:
    """Append-only JSONL audit events; secrets redacted before write."""

    path: Path
    session_id: str
    workspace: Path
    profile: str
    redact: bool = True

    def write(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "version": AUDIT_VERSION,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "profile": self.profile,
            "event": event,
        }
        record.update({key: _redact_value(value, self.redact) for key, value in fields.items()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, count: int = 20, *, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return the most recent audit events.

        When *session_id* is given the entire file is scanned so that session
        events are never missed because they sit before a long run of events
        from other sessions.  When *session_id* is None the existing global
        tail behaviour is preserved (last *count* parseable lines only).
        """
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if session_id is not None:
            # Scan all lines; a fixed pre-filter window would silently drop
            # session events that are buried more than window lines deep.
            events: list[dict[str, Any]] = []
            for line in lines:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            events = [e for e in events if e.get("session_id") == session_id]
            return events[-count:]
        # Global tail: keep existing behaviour — last count parseable lines.
        global_events: list[dict[str, Any]] = []
        for line in lines[-count:]:
            try:
                global_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return global_events
