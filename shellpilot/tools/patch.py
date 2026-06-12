"""Anchored file edits and file writes (design sections 12.4, 12.5).

The Phase 0.5 benchmark validated byte-exact span reproduction for gemma4:e4b
(docs/benchmarks/2026-06-10-gemma4-e4b.md), so the anchored strategy stands:
the model supplies only the changed span; unchanged text comes from disk after
the snapshot hash is validated. v1 ships the single-anchor operations
(replace_exact, insert_before, insert_after, delete_exact); whole-file rewrites
go through write_file(mode="overwrite") against a validated snapshot, covering
rewrite_file_from_snapshot. replace_between is deferred: it needs a two-anchor
schema and replace_exact covers its use cases at the measured reliability.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.persistence.json_store import atomic_write_text
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ToolContext, ToolResult, ToolSpec, resolve_in_workspace
from shellpilot.tools.filesystem import ALL_PROFILES, is_binary

OPERATIONS = ("replace_exact", "insert_before", "insert_after", "delete_exact")
WRITE_MODES = ("create", "overwrite", "append")
MAX_PREVIEW_LINES = 60


def apply_edit(text: str, operation: str, old: str, new: str) -> tuple[str | None, str]:
    """Apply one anchored edit; returns (new_text, error). Exactly-once anchors only."""
    if operation not in OPERATIONS:
        return None, f"unknown operation {operation!r}; use one of {', '.join(OPERATIONS)}"
    if not old:
        return None, "old must contain the exact existing text to anchor on"
    count = text.count(old)
    if count == 0:
        return None, (
            "anchor not found: the `old` text does not appear in the file. "
            "Re-read the file and copy the span byte-for-byte."
        )
    if count > 1:
        return None, (
            f"ambiguous anchor: `old` appears {count} times. "
            "Include more surrounding context so it matches exactly once."
        )
    if operation == "replace_exact":
        return text.replace(old, new, 1), ""
    if operation == "insert_before":
        i = text.index(old)
        point = text.rfind("\n", 0, i) + 1
        if not new.endswith("\n"):
            new = new + "\n"
        return text[:point] + new + text[point:], ""
    if operation == "insert_after":
        end = text.index(old) + len(old)
        if old.endswith("\n"):
            point = end
        else:
            nl = text.find("\n", end)
            if nl == -1:
                point = len(text)
                if not new.startswith("\n"):
                    new = "\n" + new
            else:
                point = nl + 1
        if not new.endswith("\n") and point < len(text):
            new = new + "\n"
        return text[:point] + new + text[point:], ""
    return text.replace(old, "", 1), ""  # delete_exact


def _read_for_edit(context: ToolContext, raw_path: str) -> tuple[Path | None, str, str]:
    """Resolve, boundary-check, snapshot-validate; returns (path, text, error)."""
    path = resolve_in_workspace(context.workspace, raw_path)
    if not path.is_file():
        return None, "", f"{raw_path} is not a file"
    if is_binary(path):
        return None, "", f"{raw_path} looks binary; refusing text edit"
    if context.snapshots is None:
        return None, "", "snapshot store unavailable; cannot edit safely"
    stale = context.snapshots.validate(path)
    if stale:
        return None, "", stale
    data = path.read_bytes()
    try:
        return path, data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "", f"{raw_path} is not valid UTF-8; refusing text edit"


def _write_preserving(path: Path, text: str) -> None:
    """Atomic write that preserves the original file mode."""
    mode = path.stat().st_mode if path.exists() else None
    atomic_write_text(path, text)
    if mode is not None:
        os.chmod(path, mode)


def unified_diff(path: Path, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )
    if len(lines) > MAX_PREVIEW_LINES:
        lines = lines[:MAX_PREVIEW_LINES] + [f"... ({len(lines) - MAX_PREVIEW_LINES} more lines)\n"]
    return "".join(lines)


def _patch_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path, text, error = _read_for_edit(context, str(arguments["path"]))
    if path is None:
        return ToolResult(success=False, summary=error, content=error)
    new_text, edit_error = apply_edit(
        text,
        str(arguments["operation"]),
        str(arguments["old"]),
        str(arguments.get("new", "")),
    )
    if new_text is None:
        return ToolResult(success=False, summary="edit rejected", content=edit_error)
    _write_preserving(path, new_text)
    assert context.snapshots is not None
    context.snapshots.record(path, new_text.encode("utf-8"))
    diff = unified_diff(path, text, new_text)
    return ToolResult(
        success=True,
        summary=f"patched {arguments['path']} ({arguments['operation']})",
        content=f"Edit applied. Diff:\n{diff}",
        side_effect=SideEffect.WORKSPACE_WRITE,
        risk=RiskLevel.MEDIUM,
        metadata={"path": str(path), "diff": diff},
    )


def _patch_preview(context: ToolContext, arguments: dict[str, Any]) -> str:
    path, text, error = _read_for_edit(context, str(arguments["path"]))
    if path is None:
        return f"(cannot preview: {error})"
    new_text, edit_error = apply_edit(
        text,
        str(arguments["operation"]),
        str(arguments["old"]),
        str(arguments.get("new", "")),
    )
    if new_text is None:
        return f"(cannot preview: {edit_error})"
    return unified_diff(path, text, new_text)


def _write_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    raw_path = str(arguments["path"])
    mode = str(arguments.get("mode", "create"))
    if mode not in WRITE_MODES:
        return ToolResult(
            success=False,
            summary=f"unknown mode {mode!r}",
            content=f"mode must be one of {', '.join(WRITE_MODES)}",
        )
    path = resolve_in_workspace(context.workspace, raw_path)
    content = str(arguments["content"])
    exists = path.exists()

    if mode == "create":
        if exists:
            return ToolResult(
                success=False,
                summary=f"{raw_path} already exists",
                content="use mode=overwrite (after reading the file) or patch_file instead",
            )
        before = ""
        new_text = content
    else:
        if not exists:
            return ToolResult(
                success=False,
                summary=f"{raw_path} does not exist",
                content="use mode=create for new files",
            )
        if is_binary(path):
            return ToolResult(
                success=False, summary=f"{raw_path} looks binary; refusing text write", content=""
            )
        if context.snapshots is None:
            return ToolResult(success=False, summary="snapshot store unavailable", content="")
        stale = context.snapshots.validate(path)
        if stale:
            return ToolResult(success=False, summary="stale write rejected", content=stale)
        before = path.read_bytes().decode("utf-8", errors="replace")
        new_text = before + content if mode == "append" else content

    _write_preserving(path, new_text)
    if context.snapshots is not None:
        context.snapshots.record(path, new_text.encode("utf-8"))
    diff = unified_diff(path, before, new_text)
    return ToolResult(
        success=True,
        summary=f"wrote {raw_path} ({mode}, {len(new_text)} chars)",
        content=f"Write applied. Diff:\n{diff}",
        side_effect=SideEffect.WORKSPACE_WRITE,
        risk=RiskLevel.MEDIUM,
        metadata={"path": str(path), "diff": diff},
    )


def _write_preview(context: ToolContext, arguments: dict[str, Any]) -> str:
    raw_path = str(arguments["path"])
    mode = str(arguments.get("mode", "create"))
    try:
        path = resolve_in_workspace(context.workspace, raw_path)
    except Exception as exc:  # noqa: BLE001 - preview must never raise
        return f"(cannot preview: {exc})"
    content = str(arguments.get("content", ""))
    if mode == "create" or not path.exists():
        before = ""
        after = content
    else:
        before = path.read_bytes().decode("utf-8", errors="replace")
        after = before + content if mode == "append" else content
    return unified_diff(path, before, after)


PATCH_FILE = ToolSpec(
    definition=ToolDefinition(
        name="patch_file",
        description=(
            "Edit a file with an anchored operation. `old` must be copied "
            "byte-for-byte from the file and match exactly once. Operations: "
            "replace_exact (old->new), insert_before / insert_after (new is "
            "inserted as its own line(s) before/after the LINE containing the "
            "anchor; a trailing newline is added to new if missing -- for edits "
            "within a line use replace_exact), delete_exact. "
            "The file must have been read with read_file first."
        ),
        parameters={
            "path": {"type": "string", "description": "File to edit."},
            "operation": {
                "type": "string",
                "description": "replace_exact | insert_before | insert_after | delete_exact",
            },
            "old": {"type": "string", "description": "Exact existing text (the anchor)."},
            "new": {"type": "string", "description": "Replacement or inserted text."},
        },
        required=("path", "operation", "old"),
    ),
    side_effect=SideEffect.WORKSPACE_WRITE,
    default_risk=RiskLevel.MEDIUM,
    allowed_profiles=ALL_PROFILES,
    handler=_patch_file,
    preview=_patch_preview,
)

WRITE_FILE = ToolSpec(
    definition=ToolDefinition(
        name="write_file",
        description=(
            "Write a file: mode=create for new files, mode=overwrite to replace "
            "a file you have read (whole-file rewrite), mode=append to add to it. "
            "Content is written verbatim as raw text -- use real newlines, not "
            "escaped \\n sequences."
        ),
        parameters={
            "path": {"type": "string", "description": "File to write."},
            "content": {"type": "string", "description": "Text content."},
            "mode": {"type": "string", "description": "create | overwrite | append"},
        },
        required=("path", "content"),
    ),
    side_effect=SideEffect.WORKSPACE_WRITE,
    default_risk=RiskLevel.MEDIUM,
    allowed_profiles=ALL_PROFILES,
    handler=_write_file,
    preview=_write_preview,
)
