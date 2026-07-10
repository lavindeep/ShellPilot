"""Memory tools: read freely, write only through proposal + approval (section 16.3).

memory_read is a normal read-only tool. memory_propose_update is the single
write path the model has: it goes through the standard broker approval flow
(MEDIUM risk, ask in every profile) with a pseudo-diff preview, and the
handler only runs after the user approves. The model never touches the memory
files directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.memory.store import MemoryFormatError, MemoryStores
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec

PREVIEW_TOKENS = 2000

MEMORY_SCOPE_POLICY = (
    "Global memory: durable user facts and cross-project preferences, including name, "
    "timezone, role, preferred languages/tools, collaboration style, and defaults the "
    "user wants everywhere. Project memory: current workspace facts/preferences, "
    "including repo commands, paths, architecture, dependencies, conventions, "
    "team/product facts, and preferences that apply only here. Facts about the user's "
    "skills or identity are global unless explicitly project-scoped. Project memory "
    "overrides global memory only inside this workspace. Do not store secrets, "
    "credentials, one-off task details, guesses, temporary context, or file contents "
    "the user did not ask to remember. If scope is ambiguous, prefer project memory "
    "when global storage could pollute other projects; ask only when the distinction "
    "matters."
)


def _diff_preview(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"--- a/{title}\n+++ b/{title}\n@@ -1 +1 @@\n{body}\n"


def make_memory_tools(get_stores: Callable[[], MemoryStores]) -> list[ToolSpec]:
    """Build memory tools that resolve stores through *get_stores* each call.

    Handlers must not close over a concrete ``MemoryStores`` instance: ``/cwd``
    replaces the runtime's project store, and a captured reference would keep
    reading and writing the previous workspace.
    """

    def _read(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        stores = get_stores()
        block = stores.render(max_tokens=PREVIEW_TOKENS)
        if not block:
            return ToolResult(
                success=True, summary="memory is empty", content="No stored memory yet."
            )
        entries = len(stores.global_store.preferences) + len(stores.project_store.preferences)
        entries += len(stores.global_store.facts) + len(stores.project_store.facts)
        return ToolResult(success=True, summary=f"{entries} memory entries", content=block)

    def _propose(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        stores = get_stores()
        action = str(arguments.get("action", ""))
        try:
            if action == "add_preference":
                text = str(arguments.get("text", "")).strip()
                if not text:
                    return ToolResult(
                        success=False, summary="text is required", content="Provide text."
                    )
                scope = str(arguments.get("scope", "global"))
                store = stores.global_store if scope == "global" else stores.project_store
                preference = store.add_preference(text, scope=scope, source="assistant")
                return ToolResult(
                    success=True,
                    summary=f"saved {preference.id}",
                    content=f"Preference saved as {preference.id} (scope {scope}).",
                )
            if action == "add_fact":
                kind = str(arguments.get("kind", "")).strip()
                value = str(arguments.get("value", "")).strip()
                label = str(arguments.get("label", "")).strip()
                if not (kind and value and label):
                    return ToolResult(
                        success=False,
                        summary="kind, value, and label are required",
                        content="Provide kind, value, and label for the fact.",
                    )
                fact = stores.project_store.add_fact(
                    kind=kind, value=value, label=label, source="assistant", confidence="observed"
                )
                return ToolResult(
                    success=True,
                    summary=f"saved {fact.id}",
                    content=f"Project fact saved as {fact.id}.",
                )
            if action == "forget":
                entry_id = str(arguments.get("id", "")).strip()
                owner = stores.find_store(entry_id) if entry_id else None
                if owner is None:
                    return ToolResult(
                        success=False,
                        summary=f"no memory entry {entry_id!r}",
                        content="Use memory_read to list entries and their ids.",
                    )
                owner.remove(entry_id)
                return ToolResult(
                    success=True, summary=f"forgot {entry_id}", content=f"Removed {entry_id}."
                )
        except MemoryFormatError as exc:
            return ToolResult(success=False, summary=str(exc), content=str(exc))
        return ToolResult(
            success=False,
            summary=f"unknown action {action!r}",
            content="action must be add_preference, add_fact, or forget.",
        )

    def _propose_preview(context: ToolContext, arguments: dict[str, Any]) -> str:
        stores = get_stores()
        action = str(arguments.get("action", ""))
        if action == "add_preference":
            scope = str(arguments.get("scope", "global"))
            return _diff_preview(
                f"memory ({scope})", [f"+ {str(arguments.get('text', '')).strip()}"]
            )
        if action == "add_fact":
            label = str(arguments.get("label", "")).strip()
            value = str(arguments.get("value", "")).strip()
            kind = str(arguments.get("kind", "")).strip()
            return _diff_preview("memory (project)", [f"+ ({kind}) {label}: {value}"])
        if action == "forget":
            entry_id = str(arguments.get("id", "")).strip()
            store = stores.find_store(entry_id)
            if store is None:
                return f"(no memory entry {entry_id!r})"
            text = next(
                (p.text for p in store.preferences if p.id == entry_id),
                next(
                    (f"({f.kind}) {f.label}: {f.value}" for f in store.facts if f.id == entry_id),
                    "",
                ),
            )
            return _diff_preview("memory", [f"- [{entry_id}] {text}"])
        return ""

    memory_read = ToolSpec(
        definition=ToolDefinition(
            name="memory_read",
            description="List stored behavior preferences and project facts with their ids.",
            parameters={},
            required=(),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_read,
    )

    memory_propose_update = ToolSpec(
        definition=ToolDefinition(
            name="memory_propose_update",
            description=(
                "Propose a memory update for user approval. "
                "action=add_preference (text, optional scope: global|project), "
                "action=add_fact (kind, label, value), or action=forget (id). "
                "The user sees and approves every update; never assume it was saved. "
                f"{MEMORY_SCOPE_POLICY}"
            ),
            parameters={
                "action": {
                    "type": "string",
                    "description": "add_preference | add_fact | forget",
                },
                "text": {"type": "string", "description": "Preference text."},
                "scope": {"type": "string", "description": "global | project"},
                "kind": {"type": "string", "description": "Fact kind, e.g. command, path."},
                "label": {"type": "string", "description": "Short fact label."},
                "value": {"type": "string", "description": "Fact value."},
                "id": {"type": "string", "description": "Entry id to forget."},
            },
            required=("action",),
        ),
        side_effect=SideEffect.WORKSPACE_WRITE,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=ALL_PROFILES,
        handler=_propose,
        preview=_propose_preview,
    )

    return [memory_read, memory_propose_update]
