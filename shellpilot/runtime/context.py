"""Structured system-prompt assembly (design section 10.5).

The runtime builds the system prompt from a fixed set of ordered blocks: the
base prompt, optional behavior instructions, optional memory, the planning
guidance, and (when a plan is live) a compact plan-state block. This module
captures that assembly as data — a ``ContextSnapshot`` of ``ContextBlock``s —
so a single source of truth feeds both the live model prompt and the
``/context`` breakdown. The assembler is pure: it performs no file or model
I/O; callers render their inputs (behavior block, memory block, plan state)
and pass the finished text in.
"""

from __future__ import annotations

from dataclasses import dataclass

from shellpilot.runtime.budget import estimate_tokens


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


class ContextAssembler:
    """Pure (no I/O). Builds the structured system-prompt snapshot."""

    def assemble(
        self,
        *,
        base_prompt: str,
        behavior_block: str,
        memory_block: str,
        planning_guidance: str,
        plan_state: str,
    ) -> ContextSnapshot:
        """Build the snapshot from already-rendered block texts.

        Order is load-bearing and must match the legacy concatenation: base
        prompt, behavior, memory, planning guidance, plan state. Behavior,
        memory, and plan state are injected only when non-empty; planning
        guidance is always injected.
        """
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
            ContextBlock(
                name="planning guidance",
                source="prompts",
                text=planning_guidance,
                injected=True,
            ),
            ContextBlock(
                name="plan state",
                source="plan",
                text=plan_state,
                injected=bool(plan_state),
            ),
        )
        return ContextSnapshot(blocks=blocks)
