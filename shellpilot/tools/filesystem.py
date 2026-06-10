"""Read-only filesystem tools: read_file and list_dir (design section 12.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.budget import truncate_to_tokens
from shellpilot.tools.base import (
    ToolContext,
    ToolResult,
    ToolSpec,
    resolve_in_workspace,
)

DEFAULT_MAX_LINES = 200
MAX_DIR_ENTRIES = 500
_BINARY_SNIFF_BYTES = 8192

ALL_PROFILES = frozenset({"supervised", "balanced"})


def is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(_BINARY_SNIFF_BYTES)


def _read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = resolve_in_workspace(context.workspace, str(arguments["path"]))
    if not path.is_file():
        return ToolResult(success=False, summary=f"{arguments['path']} is not a file", content="")
    if is_binary(path):
        return ToolResult(
            success=False,
            summary=f"{arguments['path']} looks binary; refusing text read",
            content="",
        )
    start_line = int(arguments.get("start_line", 1))
    max_lines = int(arguments.get("max_lines", DEFAULT_MAX_LINES))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[start_line - 1 : start_line - 1 + max_lines]
    body = "\n".join(window)
    bounded, truncated = truncate_to_tokens(body, context.max_result_tokens)
    end_line = start_line + len(window) - 1
    return ToolResult(
        success=True,
        summary=f"read {arguments['path']} lines {start_line}-{end_line} of {len(lines)}",
        content=bounded,
        truncated=truncated or len(window) < len(lines),
        metadata={"path": str(path), "total_lines": str(len(lines))},
    )


def _list_dir(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = resolve_in_workspace(context.workspace, str(arguments["path"]))
    if not path.is_dir():
        return ToolResult(
            success=False, summary=f"{arguments['path']} is not a directory", content=""
        )
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    shown = entries[:MAX_DIR_ENTRIES]
    rows = [
        f"{entry.name}/" if entry.is_dir() else f"{entry.name} ({entry.stat().st_size} bytes)"
        for entry in shown
    ]
    body, truncated = truncate_to_tokens("\n".join(rows), context.max_result_tokens)
    return ToolResult(
        success=True,
        summary=f"{len(entries)} entries in {arguments['path']}",
        content=body,
        truncated=truncated or len(shown) < len(entries),
    )


READ_FILE = ToolSpec(
    definition=ToolDefinition(
        name="read_file",
        description=(
            "Read bounded text content from a file in the workspace. "
            "Use start_line and max_lines for windows into large files."
        ),
        parameters={
            "path": {"type": "string", "description": "File path, relative to the workspace."},
            "start_line": {"type": "integer", "description": "First line to read (1-based)."},
            "max_lines": {"type": "integer", "description": "Maximum lines to return."},
        },
        required=("path",),
    ),
    side_effect=SideEffect.NONE,
    default_risk=RiskLevel.LOW,
    allowed_profiles=ALL_PROFILES,
    handler=_read_file,
)

LIST_DIR = ToolSpec(
    definition=ToolDefinition(
        name="list_dir",
        description="List the entries of a directory in the workspace.",
        parameters={
            "path": {"type": "string", "description": "Directory path, relative to workspace."}
        },
        required=("path",),
    ),
    side_effect=SideEffect.NONE,
    default_risk=RiskLevel.LOW,
    allowed_profiles=ALL_PROFILES,
    handler=_list_dir,
)
