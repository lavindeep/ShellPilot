"""Structured system-prompt assembly (design section 10.5).

The runtime builds the system prompt from a fixed set of ordered blocks: the
base prompt, optional behavior instructions, optional memory, conditional skill
blocks (with a skills-index block when at least one skill body is injected),
and (when a plan is live) a compact plan-state block. This module captures that
assembly as data — a ``ContextSnapshot`` of ``ContextBlock``s — so a single
source of truth feeds both the live model prompt and the ``/context``
breakdown. The assembler is pure: it performs no file or model I/O; callers
render their inputs (behavior block, memory block, plan state) and pass the
finished texts and the discovered skills in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shellpilot.runtime.budget import estimate_tokens
from shellpilot.skills.model import Skill, SkillTrigger
from shellpilot.skills.triggers import TriggerContext, any_fires


@dataclass(frozen=True)
class ContextBlock:
    """One ordered piece of the system prompt and whether it is injected."""

    name: str
    source: str
    text: str
    injected: bool
    reason: str = ""

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class ContextSnapshot:
    """The full ordered set of context blocks for one assembled prompt."""

    blocks: tuple[ContextBlock, ...]

    def system_text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.injected)

    @property
    def est_system_tokens(self) -> int:
        return estimate_tokens(self.system_text())


def _skill_block_text(skill: Skill) -> str:
    return f"## Skill: {skill.name}\n{skill.body}"


def _skill_should_inject(skill: Skill, *, ctx: TriggerContext) -> bool:
    """Trigger predicate used by both the live prompt and /skills Active column."""
    return any_fires(skill.triggers, skill.name, ctx)


def _skill_not_injected_reason(skill: Skill, *, ctx: TriggerContext) -> str:
    if any(
        trigger in (SkillTrigger.PLAN_PROPOSED, SkillTrigger.PLAN_ACTIVE, SkillTrigger.PLAN_BLOCKED)
        for trigger in skill.triggers
    ):
        return "plan not active" if ctx.plan_status is None else "plan state mismatch"
    if SkillTrigger.WEB_ENABLED in skill.triggers:
        return "web disabled"
    return "disabled"


def _ordered_valid_skills(skills: Sequence[Skill]) -> list[Skill]:
    """Valid skills only, planning first, then the rest alphabetical by name."""
    valid = [s for s in skills if s.valid]
    return sorted(valid, key=lambda s: (s.name != "planning", s.name))


class ContextAssembler:
    """Pure (no I/O). Builds the structured system-prompt snapshot."""

    def assemble(
        self,
        *,
        base_prompt: str,
        behavior_block: str,
        memory_block: str,
        skills: Sequence[Skill],
        enabled: tuple[str, ...],
        skill_token_budget: int,
        plan_state: str,
    ) -> ContextSnapshot:
        """Build the snapshot from already-rendered block texts plus the
        discovered skills.

        Order is load-bearing: base prompt, behavior, memory, the skills group
        (skills-index block then skill bodies), plan state. Behavior, memory,
        and plan state are injected only when non-empty.

        Skill injection is deterministic. Only valid skills get blocks (planning
        first, then alphabetical). A skill is injected when any of its triggers
        fires and the cumulative skill-body token budget is not yet exceeded.
        Non-injected valid skills still appear as blocks (``injected`` False,
        ``reason`` set) so ``/context`` explains itself. The ``skills index``
        block is injected only when at least one skill body is injected this
        turn.
        """
        ctx = TriggerContext(
            plan_status="active" if plan_state else None,
            web_enabled=False,
            enabled=enabled,
        )

        skill_blocks: list[ContextBlock] = []
        injected_names: list[str] = []
        cumulative = 0
        budget_blown = False
        for skill in _ordered_valid_skills(skills):
            text = _skill_block_text(skill)
            source = skill.root
            if not _skill_should_inject(skill, ctx=ctx):
                reason = _skill_not_injected_reason(skill, ctx=ctx)
                skill_blocks.append(
                    ContextBlock(
                        name=f"skill:{skill.name}",
                        source=source,
                        text=text,
                        injected=False,
                        reason=reason,
                    )
                )
                continue
            if budget_blown or cumulative + skill.est_tokens > skill_token_budget:
                budget_blown = True
                skill_blocks.append(
                    ContextBlock(
                        name=f"skill:{skill.name}",
                        source=source,
                        text=text,
                        injected=False,
                        reason="skipped: skill budget",
                    )
                )
                continue
            cumulative += skill.est_tokens
            injected_names.append(skill.name)
            skill_blocks.append(
                ContextBlock(
                    name=f"skill:{skill.name}",
                    source=source,
                    text=text,
                    injected=True,
                )
            )

        index_injected = bool(injected_names)
        index_block = ContextBlock(
            name="skills index",
            source="skills",
            text=f"Loaded skills: {', '.join(injected_names)}.",
            injected=index_injected,
            reason="" if index_injected else "no skill bodies injected",
        )

        blocks = (
            ContextBlock(
                name="base prompt",
                source="system",
                text=base_prompt,
                injected=True,
            ),
            ContextBlock(
                name="behavior",
                source="behavior:AGENTS.md",
                text=behavior_block,
                injected=bool(behavior_block),
            ),
            ContextBlock(
                name="memory",
                source="memory",
                text=memory_block,
                injected=bool(memory_block),
            ),
            index_block,
            *skill_blocks,
            ContextBlock(
                name="plan state",
                source="plan",
                text=plan_state,
                injected=bool(plan_state),
            ),
        )
        return ContextSnapshot(blocks=blocks)
