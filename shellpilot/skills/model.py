"""Data model for discovered skills (design section 23)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SkillTrigger(Enum):
    ALWAYS = "always"  # user/builtin general skills: injected every turn when enabled
    # builtin planning skill: injected only while a plan is active/blocked
    PLAN_ACTIVE = "plan_active"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str  # already token-bounded
    root: str  # "builtin" | "user"
    trigger: SkillTrigger
    est_tokens: int
    valid: bool = True
    error: str = ""  # invalid reason, or an advisory note on a valid skill
