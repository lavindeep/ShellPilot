"""Golden checks for the main system prompt (design section 26.1)."""

from pathlib import Path

from shellpilot.prompts.system import _BASE, PROMPT_VERSION, build_system_prompt
from shellpilot.skills.loader import discover_skills
from shellpilot.skills.model import Skill, SkillTrigger


def _planning_skill() -> Skill:
    """Load the real builtin planning skill body from the package."""
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        enabled=(),
        max_tokens=800,
    )
    planning = next(s for s in skills if s.root == "builtin" and s.name == "planning")
    return planning


def test_prompt_includes_core_themes() -> None:
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    for phrase in (
        "local",
        "Answer questions directly",
        "plan",
        "Never hide a shell command",
        "runtime owns approvals",
        "secrets",
        "/work",
        "balanced",
    ):
        assert phrase in prompt, f"missing theme: {phrase}"


def test_prompt_appends_behavior_block() -> None:
    prompt = build_system_prompt(
        workspace=Path("/work"),
        profile="supervised",
        behavior_block="## User behavior instructions (global)\nBe terse.",
    )
    assert prompt.endswith("Be terse.")


def test_system_prompt_forbids_prose_plans() -> None:
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "Never write a plan" in prompt
    assert "never ask for approval in prose" in prompt


def test_system_prompt_instructs_same_turn_continuation() -> None:
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "same turn" in prompt


def test_planning_skill_excludes_trivial_tasks() -> None:
    # Proposal-time rule moved into the base prompt; execution discipline is the
    # planning skill. The trivial-task rule is proposal-time, so it lives in base.
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "Do NOT plan trivial tasks" in prompt


def test_planning_skill_one_plan_rule() -> None:
    # Folding all setup into one plan is proposal-time → base prompt.
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "Fold ALL related setup into that one plan" in prompt


def test_planning_skill_body_carries_execution_discipline() -> None:
    # Execution-time mechanics (update_plan, blocker protocol) live in the skill.
    body = _planning_skill().body
    assert "update_plan(step=1, status=" in body
    assert 'update_plan(blocker="<evidence>")' in body
    assert "same turn continues" in body


def test_base_prompt_retains_proposal_rules() -> None:
    # No-regression gate: base prompt owns proposal-time discipline and carries
    # the single bridge sentence, but holds NO update_plan execution mechanics.
    assert "3 or more distinct" in _BASE
    assert "Do NOT plan trivial tasks" in _BASE
    assert "Never write a plan" in _BASE
    assert "After a plan is approved, keep working in this same turn" in _BASE
    assert "update_plan" not in _BASE


def test_prompt_version_bumped() -> None:
    assert PROMPT_VERSION >= 3


def test_prompt_network_statement_is_accurate() -> None:
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "no independent network access" in prompt.lower()


def test_planning_skill_trigger_is_plan_active() -> None:
    assert _planning_skill().trigger is SkillTrigger.PLAN_ACTIVE
