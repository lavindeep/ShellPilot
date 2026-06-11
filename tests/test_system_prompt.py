"""Golden checks for the main system prompt (design section 26.1)."""

from pathlib import Path

from shellpilot.prompts.planning import PLANNING_GUIDANCE
from shellpilot.prompts.system import PROMPT_VERSION, build_system_prompt


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


def test_planning_guidance_excludes_trivial_tasks() -> None:
    assert "Do NOT plan trivial tasks" in PLANNING_GUIDANCE


def test_planning_guidance_one_plan_rule() -> None:
    assert "Fold ALL related setup into that one plan" in PLANNING_GUIDANCE


def test_prompt_version_bumped() -> None:
    assert PROMPT_VERSION == 2
