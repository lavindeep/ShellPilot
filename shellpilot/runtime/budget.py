"""Model-aware token budgeting (design section 10.5).

No local tokenizer in v1: all token figures are chars/4 estimates with a safety
margin, and compaction triggers before the hard limit to absorb estimation error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shellpilot.config.model import ContextSettings

FALLBACK_CONTEXT_TOKENS = 8192
CHARS_PER_TOKEN = 4
# Model metadata reports the model's MAXIMUM context. Adopting it verbatim as
# num_ctx would make Ollama allocate a giant KV cache on local hardware, so
# auto-detection is capped; an explicit [context] model_context_tokens setting
# is honored uncapped.
MAX_AUTO_CONTEXT_TOKENS = 32_768


def clamp(low: int, high: int, value: int) -> int:
    return max(low, min(high, value))


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ContextBudget:
    """Resolved budgets for one selected model."""

    model_context_tokens: int
    reserved_response_tokens: int
    reserved_system_tokens: int
    working_prompt_tokens: int
    compact_at_tokens: int
    hard_limit_tokens: int
    max_user_message_tokens: int
    max_tool_prompt_tokens: int
    max_total_tool_prompt_tokens: int
    max_command_prompt_tokens: int
    max_command_capture_chars: int


def resolve_budget(context: ContextSettings, detected_context_tokens: int | None) -> ContextBudget:
    """Apply the section 10.5 formulas, honoring explicit settings over auto."""
    if context.model_context_tokens:
        model_ctx = context.model_context_tokens
    elif detected_context_tokens:
        model_ctx = min(detected_context_tokens, MAX_AUTO_CONTEXT_TOKENS)
    else:
        model_ctx = FALLBACK_CONTEXT_TOKENS
    reserved_response = context.reserved_response_tokens or clamp(
        1024, 4096, math.floor(model_ctx * 0.20)
    )
    reserved_system = context.reserved_system_tokens or clamp(
        1024, 4096, math.floor(model_ctx * 0.15)
    )
    return ContextBudget(
        model_context_tokens=model_ctx,
        reserved_response_tokens=reserved_response,
        reserved_system_tokens=reserved_system,
        working_prompt_tokens=model_ctx - reserved_response - reserved_system,
        compact_at_tokens=math.floor(model_ctx * context.compact_at_ratio),
        hard_limit_tokens=math.floor(model_ctx * context.hard_limit_ratio),
        max_user_message_tokens=context.max_user_message_tokens
        or min(4096, math.floor(model_ctx * 0.25)),
        max_tool_prompt_tokens=context.max_tool_prompt_tokens
        or min(2000, math.floor(model_ctx * 0.10)),
        max_total_tool_prompt_tokens=context.max_total_tool_prompt_tokens
        or min(8000, math.floor(model_ctx * 0.30)),
        max_command_prompt_tokens=context.max_command_prompt_tokens
        or min(2000, math.floor(model_ctx * 0.10)),
        max_command_capture_chars=context.max_command_capture_chars,
    )


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """Bound text to a token budget; returns (text, was_truncated)."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text, False
    kept = text[:max_chars]
    omitted = len(text) - max_chars
    return f"{kept}\n... [truncated {omitted} chars]", True
