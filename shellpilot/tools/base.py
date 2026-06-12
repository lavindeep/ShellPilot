"""Tool interface: specs, results, argument validation, workspace boundary (section 12)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.policy.command_policy import CommandRisk
from shellpilot.policy.risk import RiskLevel, SideEffect


class ToolError(Exception):
    """A tool could not run; the message is safe to show the model."""


class WorkspaceBoundaryError(ToolError):
    """A path resolved outside the workspace boundary (design section 14.5)."""


@dataclass(frozen=True)
class ToolContext:
    """Per-turn execution context handed to tool handlers."""

    workspace: Path
    max_result_tokens: int
    max_capture_chars: int = 200_000
    emit_output: Callable[[str], None] | None = None
    snapshots: SnapshotStore | None = None
    # Privacy gate for sensitive-path contents (design section 15): one of
    # "ask" | "never" | "always". Controls whether search_text traversal reads
    # files whose path components name a credential/secret.
    allow_sensitive_reads: str = "ask"


@dataclass(frozen=True)
class ToolResult:
    """Tool output contract (design section 12.3)."""

    success: bool
    summary: str
    content: str
    truncated: bool = False
    risk: RiskLevel = RiskLevel.LOW
    side_effect: SideEffect = SideEffect.NONE
    metadata: dict[str, str] = field(default_factory=dict)


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]
ToolClassifier = Callable[[ToolContext, dict[str, Any]], CommandRisk]
ToolPreview = Callable[[ToolContext, dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: schema plus policy metadata plus handler (section 12.1)."""

    definition: ToolDefinition
    side_effect: SideEffect
    default_risk: RiskLevel
    allowed_profiles: frozenset[str]
    handler: ToolHandler
    # Argument-dependent risk (run_command); None means default_risk applies.
    classifier: ToolClassifier | None = None
    # Diff preview shown in approval prompts for write tools (section 12.5).
    preview: ToolPreview | None = None
    # Pre-approval validation hook: returns a failure message when the call can be
    # rejected deterministically before classification/approval, or None to proceed.
    precheck: Callable[[ToolContext, dict[str, Any]], str | None] | None = None

    @property
    def name(self) -> str:
        return self.definition.name

    def risk_for(self, context: ToolContext, arguments: dict[str, Any]) -> CommandRisk:
        if self.classifier is not None:
            return self.classifier(context, arguments)
        return CommandRisk(self.default_risk, ())


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
}


def validate_args(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """Returns a compact, model-readable error, or None when arguments are valid."""
    definition = spec.definition
    for name in definition.required:
        if name not in arguments:
            return f"missing required argument '{name}' for {spec.name}"
    for name, value in arguments.items():
        schema = definition.parameters.get(name)
        if schema is None:
            return f"unknown argument '{name}' for {spec.name}"
        expected = _JSON_TYPES.get(str(schema.get("type", "string")))
        if expected is not None and not isinstance(value, expected):
            if expected is int and isinstance(value, bool):
                return f"argument '{name}' must be an integer"
            return f"argument '{name}' must be of type {schema.get('type')}"
        if expected is int and isinstance(value, bool):
            return f"argument '{name}' must be an integer"
    return None


def schema_reminder(spec: ToolSpec) -> str:
    """One-line schema summary used for malformed-call recovery (section 10.4)."""
    params = ", ".join(
        f"{name}: {schema.get('type', 'string')}{'' if name in spec.definition.required else '?'}"
        for name, schema in spec.definition.parameters.items()
    )
    return f"{spec.name}({params})"


def resolve_in_workspace(workspace: Path, raw_path: str) -> Path:
    """Resolve a tool-supplied path and enforce the workspace boundary.

    Symlinks are resolved first so aliased paths cannot escape (section 24.1).
    """
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    root = workspace.resolve()
    if resolved != root and root not in resolved.parents:
        raise WorkspaceBoundaryError(
            f"path {raw_path} resolves outside the workspace boundary {root}"
        )
    return resolved
