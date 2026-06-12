"""Approval decisions by profile (design sections 14.1, 14.2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from shellpilot.policy.risk import RiskLevel, SideEffect


class Decision(Enum):
    AUTO = "auto"
    ASK = "ask"
    BLOCK = "block"


@dataclass(frozen=True)
class ApprovalRequest:
    """What the UI shows when asking the user to approve an action."""

    kind: str  # "command" | "tool"
    display: str
    risk: RiskLevel
    reasons: tuple[str, ...]
    cwd: Path
    purpose: str = ""
    diff: str = ""


def decide(
    profile: str,
    side_effect: SideEffect,
    risk: RiskLevel,
    allow_sensitive_reads: str = "ask",
) -> Decision:
    """Map profile x side-effect x risk to a decision.

    supervised: ask before every side-effecting tool and every command.
    balanced: auto-run read-only tools and low-risk commands; ask otherwise.

    A NONE-side-effect tool classified HIGH can only be a sensitive-path read
    (design section 15): the privacy gate `allow_sensitive_reads` decides it
    ("ask" -> ASK, "never" -> BLOCK, "always" -> AUTO), in every profile.
    """
    if risk is RiskLevel.BLOCKED:
        return Decision.BLOCK
    if side_effect is SideEffect.NETWORK:
        return Decision.ASK
    if side_effect is SideEffect.NONE and risk is RiskLevel.HIGH:
        if allow_sensitive_reads == "never":
            return Decision.BLOCK
        if allow_sensitive_reads == "always":
            return Decision.AUTO
        return Decision.ASK
    if profile == "supervised":
        if side_effect is SideEffect.NONE:
            return Decision.AUTO
        return Decision.ASK
    # balanced (default)
    if side_effect is SideEffect.NONE:
        return Decision.AUTO
    if risk is RiskLevel.LOW:
        return Decision.AUTO
    return Decision.ASK
