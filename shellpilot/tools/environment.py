"""Environment information tool (design section 12.2)."""

from __future__ import annotations

import platform
import sys
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ToolContext, ToolResult, ToolSpec
from shellpilot.tools.filesystem import ALL_PROFILES


def _env_info(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    lines = [
        f"os: {platform.system()} {platform.release()} ({platform.machine()})",
        f"python: {python_version}",
        f"workspace: {context.workspace}",
    ]
    return ToolResult(success=True, summary="environment info", content="\n".join(lines))


ENV_INFO = ToolSpec(
    definition=ToolDefinition(
        name="env_info",
        description="Report the operating system, Python version, and workspace directory.",
        parameters={},
        required=(),
    ),
    side_effect=SideEffect.NONE,
    default_risk=RiskLevel.LOW,
    allowed_profiles=ALL_PROFILES,
    handler=_env_info,
)
