"""Typed settings model mirroring the config.toml schema (design section 17.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

VALID_PROFILES = ("supervised", "balanced")  # trusted-local arrives in v2

TESTED_FAMILIES: Final[tuple[str, ...]] = ("gemma4", "qwen3.5")


def is_tested_model(name: str) -> bool:
    """True when the model belongs to a family ShellPilot is qualified against."""
    return any(name.startswith(family) for family in TESTED_FAMILIES)


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "ollama"
    # deprecated: ignored since v0.4.0; kept so existing configs parse without error.
    family: str = "gemma4"
    default: str = "gemma4:e4b"
    reasoning: bool = True
    base_url: str = "http://localhost:11434"
    keep_alive: str = "5m"
    # Verbatim Ollama request `options`, passed through untouched (e.g.
    # repeat_penalty, repeat_last_n, temperature, seed). ShellPilot does NOT
    # validate individual keys — Ollama validates and errors at request time.
    # num_ctx is reserved to the context budget and overrides any value here.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSettings:
    security_profile: str = "balanced"
    max_plan_steps: int = 10
    max_tool_turns: int = 40
    command_timeout_seconds: int = 600
    auto_compact: bool = True  # selective token-budget compaction (section 20.2)


@dataclass(frozen=True)
class ContextSettings:
    """Token budgets; None means "auto" (resolved from model metadata at runtime)."""

    model_context_tokens: int | None = None
    reserved_response_tokens: int | None = None
    reserved_system_tokens: int | None = None
    compact_at_ratio: float = 0.70
    hard_limit_ratio: float = 0.90
    max_user_message_tokens: int | None = None
    max_tool_prompt_tokens: int | None = None
    max_total_tool_prompt_tokens: int | None = None
    max_command_prompt_tokens: int | None = None
    max_command_capture_chars: int = 200_000


@dataclass(frozen=True)
class WorkspaceSettings:
    boundary: str = "start_cwd"
    allow_outside_workspace: bool = False


@dataclass(frozen=True)
class InstructionSettings:
    load_agents_md: bool = True


@dataclass(frozen=True)
class PrivacySettings:
    telemetry: bool = False
    redact_secrets: bool = True
    allow_sensitive_reads: str = "ask"


@dataclass(frozen=True)
class UiSettings:
    theme: str = "default"
    show_reasoning_summary: bool = True
    show_full_tool_output: bool = False
    no_color: bool = False
    glyphs: str = "auto"  # auto | unicode | ascii (design section 31.9)
    spinner: bool = True


@dataclass(frozen=True)
class ToolSettings:
    # Enabling web grounding causes network egress; it must be an explicit
    # config-file act, not something that can be toggled by an env var.
    web: bool = False


@dataclass(frozen=True)
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    instructions: InstructionSettings = field(default_factory=InstructionSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    ui: UiSettings = field(default_factory=UiSettings)
    tools: ToolSettings = field(default_factory=ToolSettings)
