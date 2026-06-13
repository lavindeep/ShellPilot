"""Tests for skill trigger predicates."""

from __future__ import annotations

from shellpilot.skills.model import SkillTrigger
from shellpilot.skills.triggers import TriggerContext, any_fires, fires


def test_fires_truth_table_per_trigger() -> None:
    active = TriggerContext(plan_status="active", web_enabled=True, enabled=("alpha",))
    proposed = TriggerContext(plan_status="proposed", web_enabled=False, enabled=())
    blocked = TriggerContext(plan_status="blocked", web_enabled=False, enabled=())
    blank = TriggerContext(plan_status=None, web_enabled=False, enabled=())

    assert fires(SkillTrigger.ALWAYS_ON, "alpha", blank) is True
    assert fires(SkillTrigger.ENABLED, "alpha", active) is True
    assert fires(SkillTrigger.ENABLED, "beta", active) is False
    assert fires(SkillTrigger.PLAN_PROPOSED, "alpha", proposed) is True
    assert fires(SkillTrigger.PLAN_PROPOSED, "alpha", active) is False
    assert fires(SkillTrigger.PLAN_ACTIVE, "alpha", active) is True
    assert fires(SkillTrigger.PLAN_ACTIVE, "alpha", blocked) is False
    assert fires(SkillTrigger.PLAN_BLOCKED, "alpha", blocked) is True
    assert fires(SkillTrigger.PLAN_BLOCKED, "alpha", blank) is False
    assert fires(SkillTrigger.WEB_ENABLED, "alpha", active) is True
    assert fires(SkillTrigger.WEB_ENABLED, "alpha", blank) is False


def test_any_fires_returns_true_when_any_trigger_matches() -> None:
    ctx = TriggerContext(plan_status="active", web_enabled=False, enabled=())

    assert any_fires(
        (SkillTrigger.PLAN_PROPOSED, SkillTrigger.PLAN_ACTIVE),
        "planning",
        ctx,
    )
    assert not any_fires(
        (SkillTrigger.PLAN_PROPOSED, SkillTrigger.WEB_ENABLED),
        "planning",
        ctx,
    )
    assert not any_fires((), "planning", ctx)
