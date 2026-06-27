"""skill_read tool: on-demand access to a skill's reference/template docs (section 23.4).

Resolution is pure exact-string matching against in-memory Skill objects.
No filesystem access, no path interpretation — a resource arg that looks like
a path simply matches nothing and returns a clean failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.skills.model import Skill
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec


def make_skill_read_tool(skills: Sequence[Skill]) -> ToolSpec:
    """Build a skill_read ToolSpec closed over *skills*.

    Only valid skills are readable: the handler filters out invalid/reserved
    skills and never advertises them, so passing the full discovered set is
    safe. Pass any sequence in tests.
    """
    # Snapshot at construction time so the closed-over list is stable.
    _skills = tuple(skills)

    def _handler(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        skill_name = str(arguments.get("skill", "")).strip()
        resource_name = str(arguments.get("resource", "")).strip()

        # Resolve skill by exact name. Invalid/reserved skills are never
        # readable — they are excluded here AND from the advertised list, so
        # this stays correct even if a caller passes the full discovered set.
        skill: Skill | None = next((s for s in _skills if s.name == skill_name and s.valid), None)
        if skill is None:
            available = ", ".join(s.name for s in _skills if s.valid) or "(none)"
            return ToolResult(
                success=False,
                summary=f"skill {skill_name!r} not found",
                content=f"Unknown skill {skill_name!r}. Available skills: {available}.",
            )

        # Resolve resource by exact .name match across references + templates.
        all_resources = skill.references + skill.templates
        resource = next((r for r in all_resources if r.name == resource_name), None)
        if resource is None:
            available = ", ".join(r.name for r in all_resources) or "(none)"
            return ToolResult(
                success=False,
                summary=f"resource {resource_name!r} not found in skill {skill_name!r}",
                content=(
                    f"Unknown resource {resource_name!r} in skill {skill_name!r}. "
                    f"Available: {available}."
                ),
            )

        return ToolResult(
            success=True,
            summary=f"read {skill_name}:{resource_name}",
            content=resource.text,
        )

    return ToolSpec(
        definition=ToolDefinition(
            name="skill_read",
            description=(
                "Open a skill's on-demand document by name and return its text. "
                "Args: skill = the skill name, resource = the document name. "
                "If a name is unknown, the available names are returned."
            ),
            parameters={
                "skill": {
                    "type": "string",
                    "description": "The skill name.",
                },
                "resource": {
                    "type": "string",
                    "description": "The document name within that skill.",
                },
            },
            required=("skill", "resource"),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_handler,
    )
