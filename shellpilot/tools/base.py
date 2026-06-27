"""Tool interface: specs, results, argument validation, workspace boundary (section 12)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shellpilot.config.model import VALID_PROFILES
from shellpilot.llm.messages import ToolDefinition
from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.policy.command_policy import CommandRisk
from shellpilot.policy.risk import RiskLevel, SideEffect

# Single canonical set derived from config.  Importing modules use this instead
# of re-stating the literal so a future profile add requires only one edit here.
ALL_PROFILES: frozenset[str] = frozenset(VALID_PROFILES)


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
    # Hard ceiling for run_command timeout; model may request shorter but never
    # longer (design section 13.1).
    command_timeout_seconds: int = 600


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
    """Returns a compact, model-readable error, or None when arguments are valid.

    Checks (in order): required presence, unknown args, top-level types (with
    bool-vs-int special case), enum membership, array item types, and integer
    bounds (minimum / maximum).
    """
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
            return f"argument '{name}' must be of type {schema.get('type')}"
        if expected is int and isinstance(value, bool):
            return f"argument '{name}' must be an integer"
        # Enum membership check.
        allowed = schema.get("enum")
        if allowed is not None and value not in allowed:
            return f"argument '{name}' must be one of: {', '.join(str(v) for v in allowed)}"
        # Array item-type check.
        if expected is list and isinstance(value, list):
            items_schema = schema.get("items")
            if items_schema is not None:
                item_type_name = str(items_schema.get("type", "string"))
                item_type = _JSON_TYPES.get(item_type_name)
                if item_type is not None:
                    for index, item in enumerate(value):
                        if not isinstance(item, item_type):
                            return (
                                f"argument '{name}' item {index} must be of type "
                                f"{item_type_name} (got {type(item).__name__})"
                            )
        # Integer bounds checks.
        if expected is int and isinstance(value, int) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if minimum is not None and value < minimum:
                return f"argument '{name}' must be >= {minimum} (got {value})"
            maximum = schema.get("maximum")
            if maximum is not None and value > maximum:
                return f"argument '{name}' must be <= {maximum} (got {value})"
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


# Honest marker shown instead of a path that resolves outside the workspace, so
# a spoofing argument never renders as a plausible in-workspace target.
OUTSIDE_WORKSPACE_DISPLAY = "<outside workspace>"


def workspace_display(workspace: Path, raw_path: str) -> str:
    """Faithful display form of a tool path argument (design section 14.5).

    NOTE (display-integrity invariant): the path shown to the user is
    derived from the SAME ``resolve_in_workspace`` the tool acts on, so the
    display can never diverge from the file actually touched. A spoofing
    argument (``..`` segments, ``./x/../y``, symlink, trailing junk) collapses
    to its resolved, workspace-relative target; an argument that escapes the
    boundary renders as an honest rejection marker, never a fabricated path.
    """
    try:
        resolved = resolve_in_workspace(workspace, raw_path)
    except WorkspaceBoundaryError:
        return OUTSIDE_WORKSPACE_DISPLAY
    relative = resolved.relative_to(workspace.resolve())
    rel_str = relative.as_posix()
    return "." if rel_str == "." else rel_str
