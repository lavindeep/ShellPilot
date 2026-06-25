"""Session transcripts: append-only JSONL per session (design section 25.2, v0.3.0).

The transcript is the full message stream, written incrementally as the
conversation happens — compaction trims the in-memory history, never the
transcript. Secrets are redacted before they touch disk. Snapshot metadata is
deliberately NOT persisted: a resumed session starts with an empty snapshot
store, so read-before-write safety forces fresh reads.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import Message, ToolCall
from shellpilot.memory.redaction import redact_secrets, redact_structure
from shellpilot.persistence.paths import project_state_dir

TOOL_EXPORT_LIMIT = 2000


@dataclass(frozen=True)
class LoadedSession:
    """A transcript read back from disk."""

    session_id: str
    model: str
    profile: str
    messages: list[Message]
    active_plan_task_id: str | None = None


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

    @staticmethod
    def recent(directory: Path, limit: int = 3) -> list[tuple[str, float]]:
        """Newest `limit` sessions as ``(label, mtime)`` pairs, newest first.

        The label is a short snippet of the session's first user message —
        the most recognizable real field — falling back to the session's model
        name when the transcript has no user message yet.
        """
        if not directory.is_dir():
            return []
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[tuple[str, float]] = []
        for path in files[:limit]:
            out.append((SessionStore._session_label(path), path.stat().st_mtime))
        return out

    @staticmethod
    def _session_label(path: Path) -> str:
        """First user message (truncated) or, failing that, the model name."""
        model = ""
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") == "meta":
                        model = record.get("model", model)
                    elif record.get("type") == "message" and record.get("role") == "user":
                        snippet = " ".join(str(record.get("content", "")).split())
                        if snippet:
                            return snippet[:32] + "…" if len(snippet) > 32 else snippet
        except OSError:
            pass
        # NOTE: no user message recorded yet — fall back to the model name
        # (the only other recognizable real field) rather than the session id.
        return model or path.stem

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
        record: dict[str, Any] = {
            "type": "message",
            "role": message.role,
            "content": content,
            "tool_calls": [
                {
                    "name": call.name,
                    "arguments": (
                        redact_structure(call.arguments) if self._redact else call.arguments
                    ),
                }
                for call in message.tool_calls
            ],
        }
        if message.images:
            record["images"] = [{"path": ref.path, "sha256": ref.sha256} for ref in message.images]
        self._append(record)

    def record_clear(self) -> None:
        self._append({"type": "clear"})

    def record_active_plan(self, task_id: str | None) -> None:
        self._append({"type": "active_plan", "task_id": task_id})

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def load(path: Path) -> LoadedSession:
        """Read a session transcript from disk and reconstruct the message history.

        Images are intentionally NOT restored: like snapshots, visual context
        does not survive a session boundary.  The model re-reads images via
        tools if needed.  Loaded messages always have images=().
        """
        model = ""
        profile = ""
        messages: list[Message] = []
        active_plan_task_id: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            if kind == "meta":
                model = record.get("model", model)
                profile = record.get("profile", profile)
            elif kind == "clear":
                messages.clear()
                active_plan_task_id = None
            elif kind == "active_plan":
                raw = record.get("task_id")
                active_plan_task_id = str(raw) if isinstance(raw, str) else None
            elif kind == "message":
                role = record.get("role")
                if not role:
                    continue
                calls = tuple(
                    ToolCall(
                        name=call.get("name", ""),
                        arguments=call.get("arguments", {}),
                    )
                    for call in record.get("tool_calls", [])
                    if isinstance(call, dict)
                )
                # images field is ignored on load — visual context is not restored.
                messages.append(
                    Message(
                        role=role,
                        content=record.get("content", ""),
                        tool_calls=calls,
                    )
                )
        return LoadedSession(
            session_id=path.stem,
            model=model,
            profile=profile,
            messages=messages,
            active_plan_task_id=active_plan_task_id,
        )


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
            lines += ["## You", "", redact_secrets(message.content), ""]
            for ref in message.images:
                lines.append(f"- image: {ref.path} (sha256 {ref.sha256[:8]}…)")
            if message.images:
                lines.append("")
        elif message.role == "assistant":
            safe_content = redact_secrets(message.content)
            if safe_content.strip():
                lines += ["## ShellPilot", "", safe_content, ""]
            for call in message.tool_calls:
                safe_args = redact_structure(call.arguments)
                arguments = json.dumps(safe_args, ensure_ascii=False, sort_keys=True)
                lines += [f"- tool call: `{call.name}({arguments})`", ""]
        elif message.role == "tool":
            content = redact_secrets(message.content)
            if len(content) > TOOL_EXPORT_LIMIT:
                omitted = len(content) - TOOL_EXPORT_LIMIT
                content = content[:TOOL_EXPORT_LIMIT] + f"\n[... {omitted} chars omitted ...]"
            lines += ["```text", content, "```", ""]
    return "\n".join(lines)
