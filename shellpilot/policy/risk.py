"""Risk and side-effect vocabulary shared by tools and policy (design sections 12, 14.2)."""

from __future__ import annotations

from enum import StrEnum


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class SideEffect(StrEnum):
    NONE = "none"
    WORKSPACE_WRITE = "workspace_write"
    VARIABLE = "variable"
    NETWORK = "network"
