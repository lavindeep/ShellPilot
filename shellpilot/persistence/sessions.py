"""Session transcripts: append-only JSONL per session (design section 25.2, v0.3.0).

The transcript is the full message stream, written incrementally as the
conversation happens — compaction trims the in-memory history, never the
transcript. Secrets are redacted before they touch disk. Snapshot metadata is
deliberately NOT persisted: a resumed session starts with an empty snapshot
store, so read-before-write safety forces fresh reads.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import Message, ToolCall
from shellpilot.memory.redaction import redact_secrets
from shellpilot.persistence.paths import project_state_dir

TOOL_EXPORT_LIMIT = 2000


@dataclass(frozen=True)
class LoadedSession:
    """A transcript read back from disk."""

    session_id: str
    model: str
    profile: str
    messages: list[Message]


class SessionStore:
    """Appends one session's transcript to `<sessions_dir>/<session_id>.jsonl`."""

    def __init__(self, directory: Path, session_id: str, *, redact: bool = True) -> None:
        self.session_id = session_id
        self.path = directory / f"{session_id}.jsonl"
        self._redact = redact

    @staticmethod
    def sessions_dir(workspace: Path) -> Path:
        return project_state_dir(workspace) / "sessions"

    @staticmethod
    def new_session_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    @staticmethod
    def latest(directory: Path) -> Path | None:
        if not directory.is_dir():
            return None
        files = sorted(directory.glob("*.jsonl"))
        return files[-1] if files else None

    @staticmethod
    def find(directory: Path, session_id: str) -> Path | None:
        candidate = directory / f"{session_id}.jsonl"
        return candidate if candidate.is_file() else None

    def write_meta(self, *, model: str, profile: str, workspace: Path) -> None:
        self._append(
            {
                "type": "meta",
                "session_id": self.session_id,
                "model": model,
                "profile": profile,
                "workspace": str(workspace),
            }
        )

    def record_message(self, message: Message) -> None:
        content = redact_secrets(message.content) if self._redact else message.content
        self._append(
            {
                "type": "message",
                "role": message.role,
                "content": content,
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments} for call in message.tool_calls
                ],
            }
        )

    def record_clear(self) -> None:
        self._append({"type": "clear"})

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def load(path: Path) -> LoadedSession:
        model = ""
        profile = ""
        messages: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record.get("type")
            if kind == "meta":
                model = record.get("model", model)
                profile = record.get("profile", profile)
            elif kind == "clear":
                messages.clear()
            elif kind == "message":
                calls = tuple(
                    ToolCall(name=call["name"], arguments=call["arguments"])
                    for call in record.get("tool_calls", [])
                )
                messages.append(
                    Message(role=record["role"], content=record["content"], tool_calls=calls)
                )
        return LoadedSession(session_id=path.stem, model=model, profile=profile, messages=messages)


def session_markdown(session: LoadedSession) -> str:
    """Markdown transcript for /export."""
    lines = [
        f"# ShellPilot session {session.session_id}",
        "",
        f"- Model: {session.model or 'unknown'}",
        f"- Profile: {session.profile or 'unknown'}",
        "",
    ]
    for message in session.messages:
        if message.role == "user":
            lines += ["## You", "", message.content, ""]
        elif message.role == "assistant":
            if message.content.strip():
                lines += ["## ShellPilot", "", message.content, ""]
            for call in message.tool_calls:
                arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
                lines += [f"- tool call: `{call.name}({arguments})`", ""]
        elif message.role == "tool":
            content = message.content
            if len(content) > TOOL_EXPORT_LIMIT:
                omitted = len(content) - TOOL_EXPORT_LIMIT
                content = content[:TOOL_EXPORT_LIMIT] + f"\n[... {omitted} chars omitted ...]"
            lines += ["```text", content, "```", ""]
    return "\n".join(lines)
