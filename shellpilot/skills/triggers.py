"""Skill trigger predicates."""

from __future__ import annotations

from dataclasses import dataclass

from shellpilot.skills.model import SkillTrigger


@dataclass(frozen=True)
class TriggerContext:
    plan_status: str | None
    web_enabled: bool
    enabled: tuple[str, ...]


def fires(trigger: SkillTrigger, skill_name: str, ctx: TriggerContext) -> bool:
    if trigger is SkillTrigger.ALWAYS_ON:
        return True
    if trigger is SkillTrigger.ENABLED:
        return skill_name in ctx.enabled
    if trigger is SkillTrigger.PLAN_PROPOSED:
        return ctx.plan_status == "proposed"
    if trigger is SkillTrigger.PLAN_ACTIVE:
        return ctx.plan_status == "active"
    if trigger is SkillTrigger.PLAN_BLOCKED:
        return ctx.plan_status == "blocked"
    if trigger is SkillTrigger.WEB_ENABLED:
        return ctx.web_enabled
    return False


def any_fires(
    triggers: tuple[SkillTrigger, ...],
    skill_name: str,
    ctx: TriggerContext,
) -> bool:
    return any(fires(trigger, skill_name, ctx) for trigger in triggers)
