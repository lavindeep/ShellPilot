"""ContextAssembler unit tests and the new assembly-contract lock.

The contract test reconstructs the expected system-prompt concatenation inline
and asserts the assembled snapshot reproduces it across the key states (bare,
behavior+memory, plan active, plan blocked). The reconstruction is the lock:
proposal-time guidance lives in the base prompt only, the builtin planning
skill body appears solely when a plan is active/blocked, and the skills-index
block appears only when at least one body is injected.
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
from shellpilot.skills.model import Skill, SkillTrigger
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
        skills=discover_skills(
            user_skills_dir=Path("/nonexistent/skills"), enabled=(), max_tokens=800
        ),
    )


def _memory_stores(tmp_path: Path) -> MemoryStores:
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json")
    global_store.add_preference("Be terse.", scope="global", source="user")
    return MemoryStores(global_store=global_store, project_store=project_store)


def _planning_body() -> str:
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"), enabled=(), max_tokens=800
    )
    return next(s for s in skills if s.root == "builtin" and s.name == "planning").body


def _expected_system_text(runtime: ConversationRuntime) -> str:
    """Inline reconstruction of the expected system-prompt concatenation.

    Order: base prompt, behavior, memory, skills-index + skill bodies (only
    while a plan is active/blocked, since planning is the sole skill here),
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
    plan_active = plan is not None and plan.status in ("active", "blocked")
    if plan_active:
        prompt = f"{prompt}\n\nLoaded skills: planning."
        prompt = f"{prompt}\n\n## Skill: planning\n{_planning_body()}"
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
    assert "Loaded skills:" not in bare_text

    # State 2: behavior + memory present (still plan-free, so no skill body).
    behavior = BehaviorInstructions(global_text="Be terse.", project_text="Use ruff.")
    enriched = _make_runtime(tmp_path, behavior=behavior, memory=_memory_stores(tmp_path))
    enriched_text = enriched.context_snapshot().system_text()
    assert enriched_text == _expected_system_text(enriched)
    assert enriched._context_snapshot().system_text() == enriched._system_message_text()
    assert "Be terse." in enriched_text
    assert "## Skill: planning" not in enriched_text

    # State 3: plan active — planning body + index + plan state all present.
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
    assert "Loaded skills: planning." in active_text
    assert "## Skill: planning" in active_text
    assert "update_plan" in active_text
    assert "Active task plan" in active_text

    # State 4: plan blocked — same injection as active.
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


# ---------------------------------------------------------------------------
# Assembler unit tests (pure, no runtime)
# ---------------------------------------------------------------------------


def _skill(
    name: str,
    *,
    root: str = "user",
    trigger: SkillTrigger = SkillTrigger.ALWAYS,
    body: str = "body",
    est_tokens: int = 1,
    valid: bool = True,
) -> Skill:
    return Skill(
        name=name,
        description="desc",
        body=body,
        root=root,
        trigger=trigger,
        est_tokens=est_tokens,
        valid=valid,
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
) -> ContextSnapshot:
    return ContextAssembler().assemble(
        base_prompt=base_prompt,
        behavior_block=behavior_block,
        memory_block=memory_block,
        skills=skills,
        enabled=enabled,
        skill_token_budget=skill_token_budget,
        plan_state=plan_state,
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
        _skill("planning", root="builtin", trigger=SkillTrigger.PLAN_ACTIVE),
        _skill("alpha"),
    )
    snapshot = _assemble(skills=skills, enabled=("zebra", "alpha"), plan_state="PS")
    skill_names = [
        b.name.removeprefix("skill:") for b in snapshot.blocks if b.name.startswith("skill:")
    ]
    assert skill_names == ["planning", "alpha", "zebra"]


def test_disabled_skill_not_injected_with_reason() -> None:
    snapshot = _assemble(skills=(_skill("alpha"),), enabled=())
    block = next(b for b in snapshot.blocks if b.name == "skill:alpha")
    assert not block.injected
    assert block.reason == "disabled"


def test_plan_active_skill_not_injected_when_no_plan() -> None:
    snapshot = _assemble(
        skills=(_skill("planning", root="builtin", trigger=SkillTrigger.PLAN_ACTIVE),),
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
