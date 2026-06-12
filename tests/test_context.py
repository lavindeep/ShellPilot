"""ContextAssembler unit tests and the byte-identity pinning lock.

The pinning test reconstructs the *legacy* system-prompt concatenation inline
and asserts the assembled snapshot reproduces it byte-for-byte across the five
states the runtime produces (bare, behavior, memory, plan active, plan
blocked). That frozen reconstruction is the lock: it must keep passing even if
the assembler is refactored.
"""

from pathlib import Path

from shellpilot.config.model import Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.store import MemoryStore, MemoryStores
from shellpilot.prompts.planning import PLANNING_GUIDANCE
from shellpilot.prompts.system import build_system_prompt
from shellpilot.runtime.budget import estimate_tokens
from shellpilot.runtime.context import ContextAssembler, ContextBlock, ContextSnapshot
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.planner import compact_plan_state
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
    )


def _memory_stores(tmp_path: Path) -> MemoryStores:
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json")
    global_store.add_preference("Be terse.", scope="global", source="user")
    return MemoryStores(global_store=global_store, project_store=project_store)


def _legacy_system_text(runtime: ConversationRuntime) -> str:
    """Frozen inline reconstruction of the pre-refactor _system_message_text()."""
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
    prompt = f"{prompt}\n\n{PLANNING_GUIDANCE}"
    plan = runtime.plan_manager.active
    if plan is not None and plan.status in ("active", "blocked"):
        prompt = f"{prompt}\n\n{compact_plan_state(plan)}"
    return prompt


# ---------------------------------------------------------------------------
# Pinning: assembled system text is byte-identical to the legacy assembly
# ---------------------------------------------------------------------------


def test_context_snapshot_byte_identical_to_legacy_assembly(tmp_path: Path) -> None:
    # State 1: bare — no behavior, no memory, no plan.
    bare = _make_runtime(tmp_path)
    assert bare.context_snapshot().system_text() == _legacy_system_text(bare)

    # State 2: behavior present.
    behavior = BehaviorInstructions(global_text="Be terse.", project_text="Use ruff.")
    with_behavior = _make_runtime(tmp_path, behavior=behavior)
    assert with_behavior.context_snapshot().system_text() == _legacy_system_text(with_behavior)
    assert with_behavior._context_snapshot().system_text() == with_behavior._system_message_text()

    # State 3: memory present.
    with_memory = _make_runtime(tmp_path, memory=_memory_stores(tmp_path))
    assert with_memory.context_snapshot().system_text() == _legacy_system_text(with_memory)

    # State 4: plan active.
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
    assert active.context_snapshot().system_text() == _legacy_system_text(active)
    assert "Active task plan" in active.context_snapshot().system_text()

    # State 5: plan blocked.
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
    assert blocked.context_snapshot().system_text() == _legacy_system_text(blocked)


# ---------------------------------------------------------------------------
# Assembler unit tests (pure, no runtime)
# ---------------------------------------------------------------------------


def _assemble(**overrides: str) -> ContextSnapshot:
    kwargs: dict[str, str] = {
        "base_prompt": "BASE",
        "behavior_block": "",
        "memory_block": "",
        "planning_guidance": "PLAN",
        "plan_state": "",
    }
    kwargs.update(overrides)
    return ContextAssembler().assemble(**kwargs)


def test_assembler_block_names_and_order() -> None:
    snapshot = _assemble()
    assert [block.name for block in snapshot.blocks] == [
        "base prompt",
        "behavior",
        "memory",
        "planning guidance",
        "plan state",
    ]


def test_assembler_empty_behavior_and_memory_excluded() -> None:
    snapshot = _assemble()
    injected = {block.name for block in snapshot.blocks if block.injected}
    assert injected == {"base prompt", "planning guidance"}
    assert snapshot.system_text() == "BASE\n\nPLAN"


def test_assembler_includes_nonempty_behavior_and_memory() -> None:
    snapshot = _assemble(behavior_block="BEHAVE", memory_block="MEM")
    assert snapshot.system_text() == "BASE\n\nBEHAVE\n\nMEM\n\nPLAN"


def test_assembler_plan_state_only_when_present() -> None:
    without = _assemble()
    assert not next(b for b in without.blocks if b.name == "plan state").injected
    with_plan = _assemble(plan_state="PLANSTATE")
    plan_block = next(b for b in with_plan.blocks if b.name == "plan state")
    assert plan_block.injected
    assert with_plan.system_text() == "BASE\n\nPLAN\n\nPLANSTATE"


def test_planning_guidance_always_injected() -> None:
    assert next(b for b in _assemble().blocks if b.name == "planning guidance").injected


def test_block_est_tokens_matches_estimate_tokens() -> None:
    block = ContextBlock(name="x", source="s", text="abcdefgh", injected=True)
    assert block.est_tokens == estimate_tokens("abcdefgh")


def test_snapshot_est_system_tokens_matches_estimate_tokens() -> None:
    snapshot = _assemble(behavior_block="BEHAVE")
    assert snapshot.est_system_tokens == estimate_tokens(snapshot.system_text())
