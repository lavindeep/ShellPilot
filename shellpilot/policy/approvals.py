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


def decide(profile: str, side_effect: SideEffect, risk: RiskLevel) -> Decision:
    """Map profile x side-effect x risk to a decision.

    supervised: ask before every side-effecting tool and every command.
    balanced: auto-run read-only tools and low-risk commands; ask otherwise.
    """
    if risk is RiskLevel.BLOCKED:
        return Decision.BLOCK
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
