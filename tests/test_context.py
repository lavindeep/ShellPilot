"""ContextAssembler unit tests and the new assembly-contract lock.

The contract test reconstructs the expected system-prompt concatenation inline
and asserts the assembled snapshot reproduces it across the key states (bare,
behavior+memory, plan active, plan blocked). The reconstruction is the lock:
proposal-time guidance lives in the base prompt only, the builtin planning
skill body appears solely when a plan is active/blocked, the mode reference
matches the plan status, and context-management is always on.
"""

from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.store import MemoryStore, MemoryStores
from shellpilot.runtime.budget import estimate_tokens
from shellpilot.runtime.context import ContextAssembler, ContextBlock, ContextSnapshot
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.planner import compact_plan_state
from shellpilot.skills.loader import discover_skills
from shellpilot.skills.model import Skill, SkillResource, SkillScript, SkillTrigger
from shellpilot.skills.triggers import TriggerContext
from tests.fakes.fake_llm import FakeLLM
from tests.fakes.fake_ui import FakeUI


def _make_runtime(
    tmp_path: Path,
    *,
    behavior: BehaviorInstructions | None = None,
    memory: MemoryStores | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=FakeLLM(script=[]),
        settings=Settings(),
        workspace=tmp_path,
        behavior=behavior or BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        memory=memory,
        skills=discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800),
    )


def _memory_stores(tmp_path: Path) -> MemoryStores:
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json")
    global_store.add_preference("Be terse.", scope="global", source="user")
    return MemoryStores(global_store=global_store, project_store=project_store)


def _planning_body() -> str:
    skills = discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800)
    return next(s for s in skills if s.root == "builtin" and s.name == "planning").body


def _builtin_body(name: str) -> str:
    skills = discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800)
    return next(s for s in skills if s.root == "builtin" and s.name == name).body


def _planning_reference(name: str) -> str:
    skills = discover_skills(user_skills_dir=Path("/nonexistent/skills"), max_tokens=800)
    planning = next(s for s in skills if s.root == "builtin" and s.name == "planning")
    return next(reference.text for reference in planning.references if reference.name == name)


def _expected_system_text(runtime: ConversationRuntime) -> str:
    """Inline reconstruction of the expected system-prompt concatenation.

    Order: base prompt, behavior, memory, skills-index + skill bodies/references,
    plan state.
    """
    from shellpilot.prompts.system import build_system_prompt

    settings = runtime.settings
    prompt = build_system_prompt(
        workspace=runtime.status().workspace,
        profile=settings.runtime.security_profile,
        behavior_block=runtime._behavior.as_prompt_block(),
    )
    if runtime._memory is not None:
        memory_cap = max(200, runtime.budget.model_context_tokens // 16)
        memory_block = runtime._memory.render(max_tokens=memory_cap)
        if memory_block:
            prompt = f"{prompt}\n\n{memory_block}"
    plan = runtime.plan_manager.active
    plan_mode = plan.status if plan is not None else None
    if plan_mode in ("active", "blocked"):
        prompt = f"{prompt}\n\nLoaded skills: planning, context-management."
        prompt = f"{prompt}\n\n## Skill: planning\n{_planning_body()}"
        prompt = f"{prompt}\n\n{_planning_reference(plan_mode)}"
        prompt = f"{prompt}\n\n## Skill: context-management\n{_builtin_body('context-management')}"
    elif plan_mode == "proposed":
        prompt = f"{prompt}\n\nLoaded skills: planning, context-management."
        prompt = f"{prompt}\n\n## Skill: planning\n{_planning_body()}"
        prompt = f"{prompt}\n\n{_planning_reference('proposed')}"
        prompt = f"{prompt}\n\n## Skill: context-management\n{_builtin_body('context-management')}"
    else:
        prompt = f"{prompt}\n\nLoaded skills: context-management."
        prompt = f"{prompt}\n\n## Skill: context-management\n{_builtin_body('context-management')}"
    if plan is not None and plan.status in ("active", "blocked"):
        prompt = f"{prompt}\n\n{compact_plan_state(plan)}"
    return prompt


# ---------------------------------------------------------------------------
# Contract: assembled system text matches the inline reconstruction
# ---------------------------------------------------------------------------


def test_context_assembly_contract(tmp_path: Path) -> None:
    # State 1: bare — no behavior, no memory, no plan. The execution mechanics
    # (update_plan) must be ABSENT on a plan-free turn.
    bare = _make_runtime(tmp_path)
    bare_text = bare.context_snapshot().system_text()
    assert bare_text == _expected_system_text(bare)
    assert "update_plan" not in bare_text
    assert "## Skill: planning" not in bare_text
    assert "Loaded skills: context-management." in bare_text
    assert "## Skill: context-management" in bare_text

    # State 2: behavior + memory present (still plan-free, so no skill body).
    behavior = BehaviorInstructions(global_text="Be terse.", project_text="Use ruff.")
    enriched = _make_runtime(tmp_path, behavior=behavior, memory=_memory_stores(tmp_path))
    enriched_text = enriched.context_snapshot().system_text()
    assert enriched_text == _expected_system_text(enriched)
    assert enriched._context_snapshot().system_text() == enriched._system_message_text()
    assert "Be terse." in enriched_text
    assert "## Skill: planning" not in enriched_text
    assert "## Skill: context-management" in enriched_text

    # State 3: plan active — planning body + active ref + index + plan state all present.
    active = _make_runtime(tmp_path)
    plan = active.plan_manager.create(
        goal="Ship it",
        user_intent="ship",
        steps=["one", "two", "three"],
        assumptions=[],
        verification=[],
    )
    active.plan_manager.approve()
    assert plan.status == "active"
    active_text = active.context_snapshot().system_text()
    assert active_text == _expected_system_text(active)
    assert "Loaded skills: planning, context-management." in active_text
    assert "## Skill: planning" in active_text
    assert "## Skill: context-management" in active_text
    assert "update_plan" in active_text
    active_blocks = {block.name for block in active.context_snapshot().blocks if block.injected}
    assert "skill:planning:reference:active.md" in active_blocks
    assert "Active task plan" in active_text

    # State 4: plan blocked — blocked reference replaces active reference.
    blocked = _make_runtime(tmp_path)
    blocked.plan_manager.create(
        goal="Ship it",
        user_intent="ship",
        steps=["one", "two", "three"],
        assumptions=[],
        verification=[],
    )
    blocked.plan_manager.approve()
    blocked.plan_manager.record_blocker("dependency missing")
    assert blocked.plan_manager.active is not None
    assert blocked.plan_manager.active.status == "blocked"
    blocked_text = blocked.context_snapshot().system_text()
    assert blocked_text == _expected_system_text(blocked)
    assert "## Skill: planning" in blocked_text
    assert 'update_plan(blocker="<evidence>")' in blocked_text
    blocked_blocks = {block.name for block in blocked.context_snapshot().blocks if block.injected}
    assert "skill:planning:reference:blocked.md" in blocked_blocks
    assert "skill:planning:reference:active.md" not in blocked_blocks
    assert "dependency missing" in blocked_text


def test_context_assembly_contract_proposed(tmp_path: Path) -> None:
    """State 5: plan proposed — planning body + proposed ref injected; no plan-state block."""
    proposed = _make_runtime(tmp_path)
    proposed.plan_manager.create(
        goal="Ship it",
        user_intent="ship",
        steps=["one", "two", "three"],
        assumptions=[],
        verification=[],
    )
    assert proposed.plan_manager.active is not None
    assert proposed.plan_manager.active.status == "proposed"

    proposed_text = proposed.context_snapshot().system_text()

    # Must match the inline reconstruction for proposed state.
    assert proposed_text == _expected_system_text(proposed)

    # Planning body and proposed reference must be injected.
    assert "## Skill: planning" in proposed_text
    assert "Loaded skills: planning, context-management." in proposed_text
    assert "## Skill: context-management" in proposed_text

    proposed_blocks = {block.name for block in proposed.context_snapshot().blocks if block.injected}
    assert "skill:planning:reference:proposed.md" in proposed_blocks
    assert "skill:planning:reference:active.md" not in proposed_blocks
    assert "skill:planning:reference:blocked.md" not in proposed_blocks

    # No plan-state block: plan_state is only rendered for active/blocked.
    plan_state_block = next(b for b in proposed.context_snapshot().blocks if b.name == "plan state")
    assert not plan_state_block.injected


# ---------------------------------------------------------------------------
# Assembler unit tests (pure, no runtime)
# ---------------------------------------------------------------------------


def _skill(
    name: str,
    *,
    root: str = "user",
    triggers: tuple[SkillTrigger, ...] = (SkillTrigger.ENABLED,),
    body: str = "body",
    est_tokens: int = 1,
    valid: bool = True,
    error: str = "",
    references: tuple[SkillResource, ...] = (),
    templates: tuple[SkillResource, ...] = (),
    scripts: tuple[SkillScript, ...] = (),
    warnings: tuple[str, ...] = (),
) -> Skill:
    return Skill(
        name=name,
        description="desc",
        body=body,
        root=root,
        triggers=triggers,
        est_tokens=est_tokens,
        valid=valid,
        error=error,
        references=references,
        templates=templates,
        scripts=scripts,
        warnings=warnings,
    )


def _reference(name: str, trigger: SkillTrigger | None, text: str | None = None) -> SkillResource:
    body = text or f"{name} guidance"
    return SkillResource(
        kind="reference",
        name=name,
        rel_path=f"references/{name}.md",
        text=body,
        est_tokens=estimate_tokens(body),
        trigger=trigger,
    )


def _template(name: str) -> SkillResource:
    text = f"{name} template"
    return SkillResource(
        kind="template",
        name=name,
        rel_path=f"templates/{name}.md",
        text=text,
        est_tokens=estimate_tokens(text),
    )


def _assemble(
    *,
    base_prompt: str = "BASE",
    behavior_block: str = "",
    memory_block: str = "",
    skills: tuple[Skill, ...] = (),
    enabled: tuple[str, ...] = (),
    skill_token_budget: int = 10_000,
    plan_state: str = "",
    trigger_ctx: TriggerContext | None = None,
) -> ContextSnapshot:
    ctx = trigger_ctx or TriggerContext(
        plan_status="active" if plan_state else None,
        web_enabled=False,
        enabled=enabled,
    )
    return ContextAssembler().assemble(
        base_prompt=base_prompt,
        behavior_block=behavior_block,
        memory_block=memory_block,
        skills=skills,
        skill_token_budget=skill_token_budget,
        plan_state=plan_state,
        trigger_ctx=ctx,
    )


def test_assembler_block_names_and_order_no_skills() -> None:
    snapshot = _assemble()
    assert [block.name for block in snapshot.blocks] == [
        "base prompt",
        "behavior",
        "memory",
        "skills index",
        "plan state",
    ]


def test_assembler_empty_behavior_and_memory_excluded() -> None:
    snapshot = _assemble()
    injected = {block.name for block in snapshot.blocks if block.injected}
    assert injected == {"base prompt"}
    assert snapshot.system_text() == "BASE"


def test_assembler_includes_nonempty_behavior_and_memory() -> None:
    snapshot = _assemble(behavior_block="BEHAVE", memory_block="MEM")
    assert snapshot.system_text() == "BASE\n\nBEHAVE\n\nMEM"


def test_assembler_plan_state_only_when_present() -> None:
    without = _assemble()
    assert not next(b for b in without.blocks if b.name == "plan state").injected
    with_plan = _assemble(plan_state="PLANSTATE")
    plan_block = next(b for b in with_plan.blocks if b.name == "plan state")
    assert plan_block.injected
    assert with_plan.system_text() == "BASE\n\nPLANSTATE"


def test_skills_index_only_when_a_body_is_injected() -> None:
    # Plan-free turn, nothing enabled → no index block injected.
    none_active = _assemble(skills=(_skill("alpha"),), enabled=())
    index = next(b for b in none_active.blocks if b.name == "skills index")
    assert not index.injected
    assert "Loaded skills:" not in none_active.system_text()

    # Enabled user skill → index lists it.
    one = _assemble(skills=(_skill("alpha"),), enabled=("alpha",))
    index = next(b for b in one.blocks if b.name == "skills index")
    assert index.injected
    assert index.text == "Loaded skills: alpha."
    assert one.system_text() == "BASE\n\nLoaded skills: alpha.\n\n## Skill: alpha\nbody"


def test_planning_first_then_alphabetical() -> None:
    skills = (
        _skill("zebra"),
        _skill(
            "planning",
            root="builtin",
            triggers=(
                SkillTrigger.PLAN_PROPOSED,
                SkillTrigger.PLAN_ACTIVE,
                SkillTrigger.PLAN_BLOCKED,
            ),
        ),
        _skill("alpha"),
    )
    snapshot = _assemble(skills=skills, enabled=("zebra", "alpha"), plan_state="PS")
    skill_names = [
        b.name.removeprefix("skill:") for b in snapshot.blocks if b.name.startswith("skill:")
    ]
    assert skill_names == ["planning", "alpha", "zebra"]
    assert [decision.skill for decision in snapshot.decisions] == ["zebra", "planning", "alpha"]


def test_disabled_skill_not_injected_with_reason() -> None:
    snapshot = _assemble(skills=(_skill("alpha"),), enabled=())
    block = next(b for b in snapshot.blocks if b.name == "skill:alpha")
    assert not block.injected
    assert block.reason == "disabled"


def test_plan_active_skill_not_injected_when_no_plan() -> None:
    snapshot = _assemble(
        skills=(
            _skill(
                "planning",
                root="builtin",
                triggers=(
                    SkillTrigger.PLAN_PROPOSED,
                    SkillTrigger.PLAN_ACTIVE,
                    SkillTrigger.PLAN_BLOCKED,
                ),
            ),
        ),
        plan_state="",
    )
    block = next(b for b in snapshot.blocks if b.name == "skill:planning")
    assert not block.injected
    assert block.reason == "plan not active"


def test_invalid_skill_gets_no_block() -> None:
    snapshot = _assemble(skills=(_skill("broken", valid=False),), enabled=("broken",))
    assert not any(b.name.startswith("skill:") for b in snapshot.blocks)


def test_skill_budget_guard_skips_with_reason() -> None:
    # Budget of 10 tokens; three 6-token skills → first injects (6), second and
    # third skipped (would exceed).
    skills = (
        _skill("alpha", est_tokens=6),
        _skill("bravo", est_tokens=6),
        _skill("charlie", est_tokens=6),
    )
    snapshot = _assemble(
        skills=skills, enabled=("alpha", "bravo", "charlie"), skill_token_budget=10
    )
    alpha = next(b for b in snapshot.blocks if b.name == "skill:alpha")
    bravo = next(b for b in snapshot.blocks if b.name == "skill:bravo")
    charlie = next(b for b in snapshot.blocks if b.name == "skill:charlie")
    assert alpha.injected
    assert not bravo.injected
    assert bravo.reason == "skipped: skill budget"
    assert not charlie.injected
    assert charlie.reason == "skipped: skill budget"
    # Index reflects only what was actually injected.
    index = next(b for b in snapshot.blocks if b.name == "skills index")
    assert index.text == "Loaded skills: alpha."


def test_block_est_tokens_matches_estimate_tokens() -> None:
    block = ContextBlock(name="x", source="s", text="abcdefgh", injected=True)
    assert block.est_tokens == estimate_tokens("abcdefgh")


def test_snapshot_est_system_tokens_matches_estimate_tokens() -> None:
    snapshot = _assemble(behavior_block="BEHAVE")
    assert snapshot.est_system_tokens == estimate_tokens(snapshot.system_text())


def test_triggered_reference_selected_by_plan_status() -> None:
    planning = _skill(
        "planning",
        root="builtin",
        triggers=(
            SkillTrigger.PLAN_PROPOSED,
            SkillTrigger.PLAN_ACTIVE,
            SkillTrigger.PLAN_BLOCKED,
        ),
        body="plan body",
        references=(
            _reference("proposed", SkillTrigger.PLAN_PROPOSED, "proposed mode"),
            _reference("active", SkillTrigger.PLAN_ACTIVE, "active mode"),
            _reference("blocked", SkillTrigger.PLAN_BLOCKED, "blocked mode"),
        ),
        est_tokens=2,
    )

    snapshot = _assemble(
        skills=(planning,),
        plan_state="PLAN",
        trigger_ctx=TriggerContext(plan_status="active", web_enabled=False, enabled=()),
    )

    injected = {block.name: block.text for block in snapshot.blocks if block.injected}
    assert injected["skill:planning"] == "## Skill: planning\nplan body"
    assert injected["skill:planning:reference:active.md"] == "active mode"
    assert "skill:planning:reference:proposed.md" not in injected
    assert "skill:planning:reference:blocked.md" not in injected
    assert snapshot.decisions[0].matched_triggers == (SkillTrigger.PLAN_ACTIVE,)
    assert snapshot.decisions[0].resource_summary == "1 ref injected (active.md)"


def test_plan_completed_does_not_inject_planning() -> None:
    planning = _skill(
        "planning",
        root="builtin",
        triggers=(SkillTrigger.PLAN_ACTIVE,),
        references=(_reference("active", SkillTrigger.PLAN_ACTIVE),),
    )

    snapshot = _assemble(
        skills=(planning,),
        trigger_ctx=TriggerContext(plan_status="completed", web_enabled=False, enabled=()),
    )

    assert not any(block.name == "skill:planning" and block.injected for block in snapshot.blocks)
    assert snapshot.decisions[0].matched_triggers == ()
    assert snapshot.decisions[0].reason == "plan state mismatch"


def test_always_on_skill_injects_without_enablement() -> None:
    snapshot = _assemble(
        skills=(_skill("context-management", root="builtin", triggers=(SkillTrigger.ALWAYS_ON,)),),
        enabled=(),
    )

    block = next(block for block in snapshot.blocks if block.name == "skill:context-management")
    assert block.injected
    assert snapshot.decisions[0].matched_triggers == (SkillTrigger.ALWAYS_ON,)


def test_enabled_skill_requires_enabled_list() -> None:
    skill = _skill("alpha", triggers=(SkillTrigger.ENABLED,))

    disabled = _assemble(skills=(skill,), enabled=())
    enabled = _assemble(skills=(skill,), enabled=("alpha",))

    assert disabled.decisions[0].reason == "disabled"
    assert enabled.decisions[0].injected is True
    assert enabled.decisions[0].matched_triggers == (SkillTrigger.ENABLED,)


def test_skill_group_budget_is_atomic_for_body_and_reference() -> None:
    skill = _skill(
        "planning",
        root="builtin",
        triggers=(SkillTrigger.PLAN_ACTIVE,),
        body="body",
        est_tokens=6,
        references=(_reference("active", SkillTrigger.PLAN_ACTIVE, "reference"),),
    )

    snapshot = _assemble(
        skills=(skill,),
        skill_token_budget=1,
        trigger_ctx=TriggerContext(plan_status="active", web_enabled=False, enabled=()),
    )

    assert not any(block.name == "skill:planning" and block.injected for block in snapshot.blocks)
    assert not any(block.name.startswith("skill:planning:reference:") for block in snapshot.blocks)
    assert snapshot.decisions[0].injected is False
    assert snapshot.decisions[0].reason == "skipped: skill budget"


def test_decisions_include_invalid_skills_and_resource_summaries() -> None:
    invalid = _skill(
        "broken",
        valid=False,
        error="reserved builtin name",
        warnings=("frontmatter name ignored",),
    )
    skill = _skill(
        "authoring",
        triggers=(SkillTrigger.ENABLED,),
        references=(_reference("route", None),),
        templates=(_template("SKILL"),),
        scripts=(
            SkillScript(
                name="doctor",
                entry="scripts/doctor.py",
                description="Check",
                mode="read",
                timeout_seconds=5,
            ),
        ),
    )

    snapshot = _assemble(skills=(invalid, skill), enabled=("authoring",))

    by_name = {decision.skill: decision for decision in snapshot.decisions}
    assert by_name["broken"].injected is False
    assert by_name["broken"].reason == "invalid: reserved builtin name"
    assert by_name["broken"].triggers == (SkillTrigger.ENABLED,)
    assert by_name["broken"].warnings == ("frontmatter name ignored",)
    assert by_name["authoring"].triggers == (SkillTrigger.ENABLED,)
    assert by_name["authoring"].resource_summary == "1 ref, 1 template"
    assert by_name["authoring"].script_summary == "1 script (execution unsupported)"
    assert not any(block.name.startswith("skill:authoring:reference:") for block in snapshot.blocks)
