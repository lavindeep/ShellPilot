"""End-to-end skill injection through the runtime (design section 23)."""

from __future__ import annotations

from pathlib import Path

from shellpilot.config.model import Settings, SkillSettings
from shellpilot.llm.messages import ToolDefinition
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.context import ContextAssembler, ContextSnapshot
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.skills.loader import SKILL_FILENAME, discover_skills
from shellpilot.skills.model import Skill, SkillResource, SkillTrigger
from shellpilot.skills.triggers import TriggerContext
from shellpilot.tools.base import ToolContext, ToolResult, ToolSpec
from shellpilot.tools.registry import default_registry
from tests.fakes.fake_llm import FakeLLM, answer, canonical_plan_call, tool_call
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
    return tuple(discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800))


def _make_skill_md(*, name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: A skill.\n---\n{body}\n"


def _user_skill(tmp_path: Path, folder: str, body: str) -> Path:
    skills_dir = tmp_path / "skills"
    d = skills_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILENAME).write_text(_make_skill_md(name=folder, body=body), encoding="utf-8")
    return skills_dir


def _system_texts(fake: FakeLLM) -> list[str]:
    return [call.messages[0].content for call in fake.calls if call.messages]


def test_planning_skill_injected_only_when_plan_active(tmp_path: Path) -> None:
    # The first model call has no active plan; subsequent calls (after approval)
    # do. The planning skill body must appear only in the post-approval calls.
    fake = FakeLLM(
        script=[
            canonical_plan_call(),
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
    discovered = tuple(discover_skills(user_skills_dir=skills_dir, max_tokens=800))
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, settings=settings, skills=discovered)

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    assert "## Skill: lint-helper" in system_text
    assert "Always run ruff before committing." in system_text
    assert "Loaded skills: context-management, lint-helper." in system_text


def test_disabled_skill_not_injected(tmp_path: Path) -> None:
    skills_dir = _user_skill(tmp_path, "lint-helper", "Always run ruff before committing.")
    # Not in the enabled list → discovered but never injected.
    discovered = tuple(discover_skills(user_skills_dir=skills_dir, max_tokens=800))
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, skills=discovered)

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    assert "## Skill: lint-helper" not in system_text
    assert "Loaded skills: context-management." in system_text

    # The /context snapshot still surfaces the block with a disabled reason.
    block = next(b for b in runtime.context_snapshot().blocks if b.name == "skill:lint-helper")
    assert not block.injected
    assert block.reason == "disabled"


def _make_stub_tool(name: str) -> ToolSpec:
    """Create a minimal stub ToolSpec for use in registry-wiring tests."""

    def _handler(ctx: ToolContext, args: dict) -> ToolResult:  # pragma: no cover
        return ToolResult(success=True, summary="stub", content="stub")

    return ToolSpec(
        definition=ToolDefinition(name=name, description=f"stub {name}"),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=frozenset({"supervised", "balanced"}),
        handler=_handler,
    )


def test_web_grounding_skill_injected_when_web_enabled(tmp_path: Path) -> None:
    """WEB_ENABLED trigger fires when both web_search and web_fetch are registered."""
    registry = default_registry()
    registry.register(_make_stub_tool("web_search"))
    registry.register(_make_stub_tool("web_fetch"))

    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        registry=registry,
        skills=_builtin_skills(),
    )

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    assert "## Skill: web-grounding" in system_text
    assert "leads, not evidence" in system_text
    assert "fetch only URLs from the search results" in system_text


def test_workflow_skill_injected_and_listed_when_enabled(tmp_path: Path) -> None:
    """Enabling a workflow builtin injects its body and lists its on-demand refs."""
    settings = Settings(skills=SkillSettings(enabled=("debugging",)))
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, settings=settings, skills=_builtin_skills())

    runtime.run_turn("there's a bug")

    system_text = fake.calls[0].messages[0].content
    # Body injected.
    assert "## Skill: debugging" in system_text
    assert "Debug by method" in system_text
    assert "debugging" in system_text
    # On-demand references advertised in the readable menu, not injected wholesale.
    assert "Readable docs (open with skill_read)" in system_text
    assert "method" in system_text
    assert "common-traps" in system_text
    # The deep reference text itself is NOT force-injected.
    assert "The Debugging Loop" not in system_text


def test_default_session_unaffected_by_workflow_skills(tmp_path: Path) -> None:
    """A default session (no skills enabled) injects no workflow skill and no menu."""
    fake = FakeLLM(script=[answer("ok")])
    ui = FakeUI()
    runtime = _make_runtime(fake, ui, tmp_path, skills=_builtin_skills())

    runtime.run_turn("hello")

    system_text = fake.calls[0].messages[0].content
    for name in ("debugging", "verification", "code-review", "git-workflow"):
        assert f"## Skill: {name}" not in system_text
    assert "Readable docs" not in system_text
    # Only the always-on builtin is loaded by default.
    assert "Loaded skills: context-management." in system_text


# ---------------------------------------------------------------------------
# Readable menu (skills readable block) — Task 2 of v0.9.0
# ---------------------------------------------------------------------------


def _assembler() -> ContextAssembler:
    return ContextAssembler()


def _on_demand_ref(name: str) -> SkillResource:
    return SkillResource(
        kind="reference",
        name=name,
        rel_path=f"references/{name}.md",
        text=f"# {name}",
        est_tokens=5,
    )


def _triggered_ref(name: str) -> SkillResource:
    return SkillResource(
        kind="reference",
        name=name,
        rel_path=f"references/{name}.md",
        text=f"# {name}",
        est_tokens=5,
        trigger=SkillTrigger.ALWAYS_ON,
    )


def _on_demand_tmpl(name: str) -> SkillResource:
    return SkillResource(
        kind="template",
        name=name,
        rel_path=f"templates/{name}.md",
        text=f"# {name}",
        est_tokens=5,
    )


def _always_on_skill(
    name: str,
    *,
    references: tuple[SkillResource, ...] = (),
    templates: tuple[SkillResource, ...] = (),
) -> Skill:
    return Skill(
        name=name,
        description="test",
        body="body text",
        root="user",
        triggers=(SkillTrigger.ALWAYS_ON,),
        est_tokens=5,
        references=references,
        templates=templates,
    )


def _assemble_bare(skills: list[Skill], *, enabled: tuple[str, ...]) -> ContextSnapshot:
    ctx = TriggerContext(plan_status=None, web_enabled=False, enabled=enabled)
    return _assembler().assemble(
        base_prompt="base",
        behavior_block="",
        memory_block="",
        skills=skills,
        skill_token_budget=8000,
        plan_state="",
        trigger_ctx=ctx,
    )


def test_readable_menu_rendered_when_opted_in() -> None:
    """Opted-in session: injected skill with on-demand refs → 'skills readable' block injected."""
    skill = _always_on_skill("my-skill", references=(_on_demand_ref("guide"),))
    snapshot = _assemble_bare([skill], enabled=("my-skill",))
    block = next((b for b in snapshot.blocks if b.name == "skills readable"), None)
    assert block is not None, "expected 'skills readable' block in snapshot"
    assert block.injected
    assert "open with skill_read" in block.text
    assert "my-skill" in block.text
    assert "guide" in block.text


def test_readable_menu_absent_default_session() -> None:
    """Default session (enabled=()): no 'skills readable' block at all."""
    # context-management is ALWAYS_ON and has on-demand refs — the gate is enabled, not the skill.
    skills = _builtin_skills()
    snapshot = _assemble_bare(list(skills), enabled=())
    names = [b.name for b in snapshot.blocks]
    assert "skills readable" not in names
    # No menu content leaks into the actual prompt on a default session.
    assert "Readable docs" not in snapshot.system_text()


def test_readable_menu_only_on_demand_listed() -> None:
    """Triggered (injected) refs must NOT appear in the menu; only trigger=None ones do."""
    skill = _always_on_skill(
        "mixed-skill",
        references=(_triggered_ref("always-injected"), _on_demand_ref("on-demand-guide")),
    )
    snapshot = _assemble_bare([skill], enabled=("mixed-skill",))
    block = next((b for b in snapshot.blocks if b.name == "skills readable"), None)
    assert block is not None
    assert "on-demand-guide" in block.text
    assert "always-injected" not in block.text


def test_readable_menu_includes_templates() -> None:
    """trigger=None templates are included in the menu alongside references."""
    skill = _always_on_skill(
        "tmpl-skill",
        templates=(_on_demand_tmpl("my-template"),),
    )
    snapshot = _assemble_bare([skill], enabled=("tmpl-skill",))
    block = next((b for b in snapshot.blocks if b.name == "skills readable"), None)
    assert block is not None
    assert "my-template" in block.text
