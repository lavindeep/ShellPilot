"""Structured memory stores (design section 16, v0.3.0).

Two JSON files share one schema: the global store (user config dir,
preferences only) and the project store (`.shellpilot/memory.json`,
preferences and facts, stamped with a project id). Explicit validation, no
pydantic; atomic writes; secrets redacted before anything reaches disk. The
model never writes these files directly — updates flow through the proposal
and approval tools (section 16.3).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shellpilot.memory.redaction import redact_secrets
from shellpilot.persistence.json_store import atomic_write_json
from shellpilot.runtime.budget import truncate_to_tokens

SCHEMA_VERSION = 1
MAX_MEMORY_FILE_CHARS = 1800
VALID_SCOPES = ("global", "project")
VALID_CONFIDENCE = ("observed", "stated", "inferred")


class MemoryFormatError(Exception):
    """A memory file is malformed or has an unsupported schema version."""


@dataclass(frozen=True)
class Preference:
    """A behavior preference (section 16.2)."""

    id: str
    scope: str
    text: str
    source: str
    updated_at: str


@dataclass(frozen=True)
class ProjectFact:
    """A durable project fact (section 16.5). The id is a v0.3.0 addition so
    /memory forget can address facts."""

    id: str
    kind: str
    value: str
    label: str
    confidence: str
    source: str


def project_id_for(workspace: Path) -> str:
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"ShellPilot:{digest}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_str(record: dict[str, Any], key: str, path: Path) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise MemoryFormatError(f"{path}: entry field {key!r} must be a string")
    return value


class MemoryStore:
    """One memory file; loads eagerly, saves atomically after every mutation."""

    def __init__(self, path: Path, *, project_id: str | None = None, redact: bool = True) -> None:
        self.path = path
        self.project_id = project_id
        self._redact = redact
        self._preferences: list[Preference] = []
        self._facts: list[ProjectFact] = []
        if path.is_file():
            self._load()

    @property
    def preferences(self) -> tuple[Preference, ...]:
        return tuple(self._preferences)

    @property
    def facts(self) -> tuple[ProjectFact, ...]:
        return tuple(self._facts)

    def reload(self) -> None:
        """Re-read the file (e.g. after the user hand-edited it via /prefs edit)."""
        self._preferences = []
        self._facts = []
        if self.path.is_file():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryFormatError(f"{self.path}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise MemoryFormatError(
                f"{self.path}: unsupported memory schema version "
                f"{data.get('version') if isinstance(data, dict) else '?'}"
            )
        for record in data.get("preferences", []):
            self._preferences.append(
                Preference(
                    id=_require_str(record, "id", self.path),
                    scope=_require_str(record, "scope", self.path),
                    text=_require_str(record, "text", self.path),
                    source=_require_str(record, "source", self.path),
                    updated_at=_require_str(record, "updated_at", self.path),
                )
            )
        for record in data.get("facts", []):
            self._facts.append(
                ProjectFact(
                    id=_require_str(record, "id", self.path),
                    kind=_require_str(record, "kind", self.path),
                    value=_require_str(record, "value", self.path),
                    label=_require_str(record, "label", self.path),
                    confidence=_require_str(record, "confidence", self.path),
                    source=_require_str(record, "source", self.path),
                )
            )

    @staticmethod
    def _next_id(prefix: str, existing: list[str]) -> str:
        highest = 0
        for entry_id in existing:
            _, _, number = entry_id.partition("_")
            if number.isdigit():
                highest = max(highest, int(number))
        return f"{prefix}_{highest + 1:03d}"

    def _clean(self, text: str) -> str:
        return redact_secrets(text) if self._redact else text

    def _payload(
        self,
        preferences: list[Preference] | None = None,
        facts: list[ProjectFact] | None = None,
    ) -> dict[str, Any]:
        stored_preferences = self._preferences if preferences is None else preferences
        stored_facts = self._facts if facts is None else facts
        payload: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "preferences": [asdict(p) for p in stored_preferences],
            "facts": [asdict(f) for f in stored_facts],
        }
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        return payload

    def _payload_text(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _current_payload_size(self) -> int:
        return len(self._payload_text(self._payload()))

    def _ensure_within_file_cap(self, payload: dict[str, Any]) -> None:
        size = len(self._payload_text(payload))
        current_size = self._current_payload_size()
        if size > MAX_MEMORY_FILE_CHARS and size <= current_size:
            return
        if size > MAX_MEMORY_FILE_CHARS:
            raise MemoryFormatError(
                f"{self.path}: memory file would be {size} characters; "
                f"limit is {MAX_MEMORY_FILE_CHARS} characters. "
                "Forget or compact existing memories before adding more."
            )

    def validate_replacement(self, preferences: list[Preference], facts: list[ProjectFact]) -> None:
        """Raise if replacing this store would violate the memory file cap."""
        self._ensure_within_file_cap(self._payload(preferences=preferences, facts=facts))

    def _save_payload(
        self,
        *,
        preferences: list[Preference] | None = None,
        facts: list[ProjectFact] | None = None,
    ) -> None:
        payload = self._payload(preferences=preferences, facts=facts)
        self._ensure_within_file_cap(payload)
        atomic_write_json(self.path, payload)

    def add_preference(self, text: str, *, scope: str, source: str) -> Preference:
        if scope not in VALID_SCOPES:
            raise MemoryFormatError(f"invalid scope {scope!r}; use one of {VALID_SCOPES}")
        preference = Preference(
            id=self._next_id("pref", [p.id for p in self._preferences]),
            scope=scope,
            text=self._clean(text),
            source=source,
            updated_at=_now_iso(),
        )
        preferences = [*self._preferences, preference]
        self._save_payload(preferences=preferences)
        self._preferences = preferences
        return preference

    def add_fact(
        self,
        *,
        kind: str,
        value: str,
        label: str,
        source: str,
        confidence: str = "stated",
    ) -> ProjectFact:
        if confidence not in VALID_CONFIDENCE:
            raise MemoryFormatError(
                f"invalid confidence {confidence!r}; use one of {VALID_CONFIDENCE}"
            )
        fact = ProjectFact(
            id=self._next_id("fact", [f.id for f in self._facts]),
            kind=kind,
            value=self._clean(value),
            label=self._clean(label),
            confidence=confidence,
            source=source,
        )
        facts = [*self._facts, fact]
        self._save_payload(facts=facts)
        self._facts = facts
        return fact

    def remove(self, entry_id: str) -> bool:
        before = len(self._preferences) + len(self._facts)
        preferences = [p for p in self._preferences if p.id != entry_id]
        facts = [f for f in self._facts if f.id != entry_id]
        if len(preferences) + len(facts) == before:
            return False
        self._save_payload(preferences=preferences, facts=facts)
        self._preferences = preferences
        self._facts = facts
        return True

    def replace_all(self, preferences: list[Preference], facts: list[ProjectFact]) -> None:
        """Wholesale replacement used by /memory compact after approval."""
        next_preferences = list(preferences)
        next_facts = list(facts)
        self.validate_replacement(next_preferences, next_facts)
        atomic_write_json(self.path, self._payload(preferences=next_preferences, facts=next_facts))
        self._preferences = next_preferences
        self._facts = next_facts

    def save(self) -> None:
        self._save_payload()


@dataclass(frozen=True)
class MemoryStores:
    """The global + project pair injected into the system prompt."""

    global_store: MemoryStore
    project_store: MemoryStore

    def render(self, max_tokens: int, *, meta: bool = False) -> str:
        """Render the memory block. ``meta`` annotates each preference with its
        (scope, source) for the ``/memory show`` view; the default is the
        injected prompt format, grouped by global/project scope."""
        global_preferences = list(self.global_store.preferences)
        project_preferences = list(self.project_store.preferences)
        facts = list(self.global_store.facts) + list(self.project_store.facts)
        if not global_preferences and not project_preferences and not facts:
            return ""
        lines = ["## Memory"]
        if global_preferences:
            lines.append("Global preferences:")
            if meta:
                lines.extend(
                    f"- [{p.id}] ({p.scope}, {p.source}) {p.text}" for p in global_preferences
                )
            else:
                lines.extend(f"- [{p.id}] {p.text}" for p in global_preferences)
        if project_preferences:
            lines.append("Project preferences:")
            if meta:
                lines.extend(
                    f"- [{p.id}] ({p.scope}, {p.source}) {p.text}" for p in project_preferences
                )
            else:
                lines.extend(f"- [{p.id}] {p.text}" for p in project_preferences)
        if facts:
            lines.append("Project facts:")
            lines.extend(f"- [{f.id}] ({f.kind}) {f.label}: {f.value}" for f in facts)
        text, _ = truncate_to_tokens("\n".join(lines), max_tokens)
        return text

    def find_store(self, entry_id: str) -> MemoryStore | None:
        for store in (self.global_store, self.project_store):
            if any(p.id == entry_id for p in store.preferences) or any(
                f.id == entry_id for f in store.facts
            ):
                return store
        return None
