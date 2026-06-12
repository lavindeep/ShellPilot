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


# A NONE-side-effect HIGH-risk tool is a sensitive-path read (design section 15);
# the allow_sensitive_reads privacy gate decides it, in every profile.
SENSITIVE_READ_MATRIX: list[tuple[str, str, Decision]] = [
    ("balanced", "ask", Decision.ASK),
    ("balanced", "never", Decision.BLOCK),
    ("balanced", "always", Decision.AUTO),
    ("supervised", "ask", Decision.ASK),
    ("supervised", "never", Decision.BLOCK),
    ("supervised", "always", Decision.AUTO),
]


@pytest.mark.parametrize(("profile", "setting", "expected"), SENSITIVE_READ_MATRIX)
def test_sensitive_read_gate(profile: str, setting: str, expected: Decision) -> None:
    decision = decide(profile, SideEffect.NONE, RiskLevel.HIGH, setting)
    assert decision is expected


def test_sensitive_read_gate_defaults_to_ask() -> None:
    # Default argument keeps existing call sites valid and ask-by-default.
    assert decide("balanced", SideEffect.NONE, RiskLevel.HIGH) is Decision.ASK


def test_plain_none_read_still_auto_in_every_mode() -> None:
    # A non-sensitive read (LOW risk) is unaffected by the privacy gate.
    for setting in ("ask", "never", "always"):
        assert decide("balanced", SideEffect.NONE, RiskLevel.LOW, setting) is Decision.AUTO
