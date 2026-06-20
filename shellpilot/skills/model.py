"""Data model for discovered skills (design section 23)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SkillTrigger(Enum):
    ALWAYS_ON = "always_on"
    ENABLED = "enabled"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_ACTIVE = "plan_active"
    PLAN_BLOCKED = "plan_blocked"
    WEB_ENABLED = "web_enabled"


@dataclass(frozen=True)
class SkillResource:
    kind: str
    name: str
    rel_path: str
    text: str
    est_tokens: int
    trigger: SkillTrigger | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("reference", "template"):
            raise ValueError("SkillResource.kind must be 'reference' or 'template'")
        if self.est_tokens < 0:
            raise ValueError("SkillResource.est_tokens must be non-negative")


@dataclass(frozen=True)
class SkillScript:
    name: str
    entry: str
    description: str
    mode: str
    timeout_seconds: int
    valid: bool = True
    error: str = ""

    def __post_init__(self) -> None:
        if self.timeout_seconds < 0:
            raise ValueError("SkillScript.timeout_seconds must be non-negative")
        if self.valid and self.error:
            raise ValueError("SkillScript.error is only valid when valid=False")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str  # already token-bounded
    root: str  # "builtin" | "user"
    triggers: tuple[SkillTrigger, ...]
    est_tokens: int
    valid: bool = True
    error: str = ""  # invalid/fatal reason
    references: tuple[SkillResource, ...] = ()
    templates: tuple[SkillResource, ...] = ()
    scripts: tuple[SkillScript, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.triggers, tuple):
            raise TypeError("Skill.triggers must be a tuple")
        if any(not isinstance(trigger, SkillTrigger) for trigger in self.triggers):
            raise TypeError("Skill.triggers must contain SkillTrigger values")
        if self.est_tokens < 0:
            raise ValueError("Skill.est_tokens must be non-negative")


def is_on_demand(resource: SkillResource) -> bool:
    # ponytail: on_demand == no trigger today; the explicit `disclosure` dial
    # for per-model profiles replaces this when that consumer ships (v0.10.x).
    return resource.trigger is None
