"""Golden checks for the main system prompt (design section 26.1)."""

from pathlib import Path

from shellpilot.prompts.system import build_system_prompt


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
