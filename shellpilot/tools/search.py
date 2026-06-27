"""Project text search tool (design section 12.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.command_policy import sensitive_path_reason
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.budget import truncate_to_tokens
from shellpilot.tools.base import (
    ALL_PROFILES,
    ToolContext,
    ToolResult,
    ToolSpec,
    resolve_in_workspace,
)
from shellpilot.tools.filesystem import _classify_path_arg, is_binary

MAX_MATCHES = 100
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".shellpilot", ".mypy_cache"}


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            if entry.is_symlink():
                # Symlinks (dir or file) can alias paths outside the workspace;
                # never traverse or read through them (mirrors the dir skip).
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files


def _search_text(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    pattern = str(arguments["pattern"])
    if not pattern:
        return ToolResult(success=False, summary="pattern must not be empty", content="")
    root = resolve_in_workspace(context.workspace, str(arguments.get("path", ".")))
    if not root.is_dir():
        return ToolResult(
            success=False, summary=f"{arguments.get('path', '.')} is not a directory", content=""
        )

    workspace_root = context.workspace.resolve()
    # An explicitly-sensitive root reaching this handler is by definition
    # authorized: in `never` mode the executor BLOCKED the call pre-handler; in
    # `ask` mode the handler only runs after the user approved; in `always` it is
    # AUTO. So the whole subtree is searched. The traversal skip below still
    # guards files DISCOVERED INCIDENTALLY under a non-sensitive root (e.g. a
    # `.env` found while searching `.`).
    root_relative = root.relative_to(workspace_root)
    root_is_sensitive = sensitive_path_reason(root_relative) is not None
    allow_sensitive = context.allow_sensitive_reads == "always" or root_is_sensitive
    matches: list[str] = []
    scanned = 0
    skipped = 0
    skipped_names: list[str] = []
    for file in _iter_files(root):
        try:
            relative = file.relative_to(workspace_root)
        except ValueError:
            # A symlink the traversal did not catch could resolve outside the
            # workspace; skip rather than crash the handler.
            continue
        if not allow_sensitive and sensitive_path_reason(relative) is not None:
            skipped += 1
            if file.name not in skipped_names and len(skipped_names) < 3:
                skipped_names.append(file.name)
            continue
        try:
            if is_binary(file):
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                matches.append(f"{relative}:{lineno}: {line.strip()[:200]}")
                if len(matches) >= MAX_MATCHES:
                    break
        if len(matches) >= MAX_MATCHES:
            break

    body, truncated = truncate_to_tokens("\n".join(matches), context.max_result_tokens)
    if skipped:
        note = (
            f"skipped {skipped} sensitive file(s) ({', '.join(skipped_names)}) — "
            'read explicitly with read_file or set privacy.allow_sensitive_reads = "always"'
        )
        body = f"{body}\n{note}" if body else note
    return ToolResult(
        success=True,
        summary=f"{len(matches)} matches for {pattern!r} in {scanned} files",
        content=body,
        truncated=truncated or len(matches) >= MAX_MATCHES,
    )


SEARCH_TEXT = ToolSpec(
    definition=ToolDefinition(
        name="search_text",
        description=(
            "Search workspace files for an exact text fragment (not a regex). "
            "Returns matching lines as path:line: text."
        ),
        parameters={
            "pattern": {"type": "string", "description": "Exact text to find."},
            "path": {"type": "string", "description": "Directory to search; default '.'"},
        },
        required=("pattern",),
    ),
    side_effect=SideEffect.NONE,
    default_risk=RiskLevel.LOW,
    allowed_profiles=ALL_PROFILES,
    handler=_search_text,
    classifier=_classify_path_arg,
)
