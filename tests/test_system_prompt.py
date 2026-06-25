"""Golden checks for the main system prompt (design section 26.1)."""

from pathlib import Path

from shellpilot.prompts.system import _BASE, PROMPT_VERSION, build_system_prompt
from shellpilot.skills.loader import discover_skills
from shellpilot.skills.model import Skill, SkillTrigger


def _planning_skill() -> Skill:
    """Load the real builtin planning skill body from the package."""
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        max_tokens=800,
    )
    planning = next(s for s in skills if s.root == "builtin" and s.name == "planning")
    return planning


def _planning_reference(name: str) -> str:
    planning = _planning_skill()
    return next(reference.text for reference in planning.references if reference.name == name)


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


def test_planning_skill_body_and_references_split_execution_discipline() -> None:
    # Mode-specific mechanics live in planning references, not the tiny body.
    body = _planning_skill().body
    assert "harness-managed plan" in body
    assert "update_plan" not in body
    assert "approval" not in body.lower()

    assert "propose_plan once" in _planning_reference("proposed")
    assert "update_plan(step=N," in _planning_reference("active")
    assert 'update_plan(blocker="<evidence>")' in _planning_reference("blocked")


def test_base_prompt_retains_proposal_rules() -> None:
    # No-regression gate: base prompt owns proposal-time discipline and carries
    # the single bridge sentence, but holds NO mode-specific execution mechanics.
    assert "propose_plan tool" in _BASE
    assert "3 or more distinct" in _BASE
    assert "Fold ALL related setup into that one plan" in _BASE
    assert "Do NOT plan trivial tasks" in _BASE
    assert "Never write a plan" in _BASE
    assert "After a plan is approved, keep working in this same turn" in _BASE
    assert "update_plan" not in _BASE
    assert "record completed steps" not in _BASE
    assert 'blocker="<evidence>"' not in _BASE
    assert "second follow-up plan" not in _BASE
    assert "Every step is an action" in _planning_reference("proposed")


def test_prompt_version_bumped() -> None:
    assert PROMPT_VERSION == 5


def test_prompts_planning_module_has_no_live_content() -> None:
    planning_py = Path(__file__).parents[1] / "shellpilot" / "prompts" / "planning.py"
    if not planning_py.exists():
        return
    text = planning_py.read_text(encoding="utf-8")
    assert "PROMPT" not in text
    assert "update_plan" not in text


def test_prompt_network_statement_is_accurate() -> None:
    # Local (default) session: the "no independent network access" claim is true.
    prompt = build_system_prompt(workspace=Path("/work"), profile="balanced")
    assert "no independent network access" in prompt.lower()
    # Egressing session: the false "entirely on this machine / no network" claim
    # must be dropped and replaced with an honest one (design section 15.2).
    remote = build_system_prompt(workspace=Path("/work"), profile="balanced", is_egressing=True)
    assert "no independent network access" not in remote.lower()
    assert "entirely on this machine" not in remote.lower()
    assert "leaves this device" in remote.lower()


def test_planning_skill_triggers_are_plan_states() -> None:
    assert _planning_skill().triggers == (
        SkillTrigger.PLAN_PROPOSED,
        SkillTrigger.PLAN_ACTIVE,
        SkillTrigger.PLAN_BLOCKED,
    )
