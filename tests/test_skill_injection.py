"""End-to-end skill injection through the runtime (design section 23)."""

from __future__ import annotations

from pathlib import Path

from shellpilot.config.model import Settings, SkillSettings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.skills.loader import SKILL_FILENAME, discover_skills
from shellpilot.skills.model import Skill
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def _make_runtime(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    skills: tuple[Skill, ...] = (),
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        skills=skills,
    )


def _builtin_skills() -> tuple[Skill, ...]:
    return tuple(
        discover_skills(user_skills_dir=Path("/nonexistent/skills"), enabled=(), max_tokens=800)
    )


def _make_skill_md(*, name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: A skill.\n---\n{body}\n"


def _user_skill(tmp_path: Path, folder: str, body: str) -> Path:
    skills_dir = tmp_path / "skills"
    d = skills_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILENAME).write_text(_make_skill_md(name=folder, body=body), encoding="utf-8")
    return skills_dir


def _plan_call() -> object:
    return tool_call(
        "propose_plan",
        goal="Add a feature",
        steps=["Inspect code", "Make change", "Run tests"],
        assumptions=["repo is clean"],
        verification=["pytest"],
    )


def _system_texts(fake: FakeLLM) -> list[str]:
    return [call.messages[0].content for call in fake.calls if call.messages]


def test_planning_skill_injected_only_when_plan_active(tmp_path: Path) -> None:
    # The first model call has no active plan; subsequent calls (after approval)
    # do. The planning skill body must appear only in the post-approval calls.
    fake = FakeLLM(
        script=[
            _plan_call(),
            tool_call("update_plan", step=1, status="completed"),
            tool_call("update_plan", step=2, status="completed"),
            tool_call("update_plan", step=3, status="completed"),
            answer("All steps done."),
        ]
    )
    ui = FakeUI(plan_answer=("y", ""))
    runtime = _make_runtime(fake, ui, tmp_path, skills=_builtin_skills())

    runtime.run_turn("Please add the feature")

    texts = _system_texts(fake)
    # First call: plan not yet proposed → no skill body.
    assert "## Skill: planning" not in texts[0]
    assert "update_plan" not in texts[0]
    # A later call (plan now active) carries the planning skill body.
    assert any("## Skill: planning" in t for t in texts[1:])
    assert any("update_plan" in t for t in texts[1:])


def test_user_skill_injected_when_enabled(tmp_path: Path) -> None:
    skills_dir = _user_skill(tmp_path, "lint-helper", "Always run ruff before committing.")
    settings = Settings(skills=SkillSettings(enabled=("lint-helper",)))
    discovered = tuple(
        discover_skills(user_skills_dir=skills_dir, enabled=("lint-helper",), max_tokens=800)
    )
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, settings=settings, skills=discovered)

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    assert "## Skill: lint-helper" in system_text
    assert "Always run ruff before committing." in system_text
    assert "Loaded skills: lint-helper." in system_text


def test_disabled_skill_not_injected(tmp_path: Path) -> None:
    skills_dir = _user_skill(tmp_path, "lint-helper", "Always run ruff before committing.")
    # Not in the enabled list → discovered but never injected.
    discovered = tuple(discover_skills(user_skills_dir=skills_dir, enabled=(), max_tokens=800))
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, skills=discovered)

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    assert "## Skill: lint-helper" not in system_text
    assert "Loaded skills:" not in system_text

    # The /context snapshot still surfaces the block with a disabled reason.
    block = next(b for b in runtime.context_snapshot().blocks if b.name == "skill:lint-helper")
    assert not block.injected
    assert block.reason == "disabled"
