"""Decision matrix tests for approvals (design section 14.1)."""

import pytest

from shellpilot.policy.approvals import Decision, decide
from shellpilot.policy.risk import RiskLevel, SideEffect

MATRIX: list[tuple[str, SideEffect, RiskLevel, Decision]] = [
    # balanced: auto read-only and low-risk; ask otherwise
    ("balanced", SideEffect.NONE, RiskLevel.LOW, Decision.AUTO),
    ("balanced", SideEffect.VARIABLE, RiskLevel.LOW, Decision.AUTO),
    ("balanced", SideEffect.VARIABLE, RiskLevel.MEDIUM, Decision.ASK),
    ("balanced", SideEffect.VARIABLE, RiskLevel.HIGH, Decision.ASK),
    ("balanced", SideEffect.WORKSPACE_WRITE, RiskLevel.MEDIUM, Decision.ASK),
    ("balanced", SideEffect.NONE, RiskLevel.BLOCKED, Decision.BLOCK),
    # supervised: ask for everything with side effects, even low risk
    ("supervised", SideEffect.NONE, RiskLevel.LOW, Decision.AUTO),
    ("supervised", SideEffect.VARIABLE, RiskLevel.LOW, Decision.ASK),
    ("supervised", SideEffect.VARIABLE, RiskLevel.HIGH, Decision.ASK),
    ("supervised", SideEffect.WORKSPACE_WRITE, RiskLevel.LOW, Decision.ASK),
    ("supervised", SideEffect.VARIABLE, RiskLevel.BLOCKED, Decision.BLOCK),
    # NETWORK: always ASK regardless of profile or risk level (privacy guarantee)
    ("balanced", SideEffect.NETWORK, RiskLevel.LOW, Decision.ASK),
    ("balanced", SideEffect.NETWORK, RiskLevel.MEDIUM, Decision.ASK),
    ("balanced", SideEffect.NETWORK, RiskLevel.HIGH, Decision.ASK),
    ("supervised", SideEffect.NETWORK, RiskLevel.LOW, Decision.ASK),
    ("supervised", SideEffect.NETWORK, RiskLevel.MEDIUM, Decision.ASK),
    ("supervised", SideEffect.NETWORK, RiskLevel.HIGH, Decision.ASK),
    # BLOCKED still wins even for NETWORK
    ("balanced", SideEffect.NETWORK, RiskLevel.BLOCKED, Decision.BLOCK),
    ("supervised", SideEffect.NETWORK, RiskLevel.BLOCKED, Decision.BLOCK),
]


@pytest.mark.parametrize(("profile", "side_effect", "risk", "expected"), MATRIX)
def test_decision_matrix(
    profile: str, side_effect: SideEffect, risk: RiskLevel, expected: Decision
) -> None:
    assert decide(profile, side_effect, risk) is expected
