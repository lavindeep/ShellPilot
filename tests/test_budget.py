"""Tests for token budgeting formulas (design section 10.5)."""

from shellpilot.config.model import ContextSettings
from shellpilot.runtime.budget import (
    estimate_tokens,
    resolve_budget,
    truncate_to_tokens,
)


def test_fallback_floor_case_matches_design_example() -> None:
    """At the 8192 fallback the numbers must match the /compact status example (§20.2)."""
    budget = resolve_budget(ContextSettings(), detected_context_tokens=None)
    assert budget.model_context_tokens == 8192
    assert budget.reserved_response_tokens == 1638  # floor(8192*0.20), inside clamp
    assert budget.reserved_system_tokens == 1228  # floor(8192*0.15), inside clamp
    assert budget.working_prompt_tokens == 8192 - 1638 - 1228
    assert budget.compact_at_tokens == 5734
    assert budget.hard_limit_tokens == 7372


def test_large_context_clamps_reservations() -> None:
    budget = resolve_budget(ContextSettings(), detected_context_tokens=131_072)
    assert budget.model_context_tokens == 32_768  # auto-detection capped
    assert budget.reserved_response_tokens == 4096  # clamped high
    assert budget.reserved_system_tokens == 4096
    assert budget.max_user_message_tokens == 4096  # min(4096, 25%)
    assert budget.max_tool_prompt_tokens == 2000
    assert budget.max_total_tool_prompt_tokens == 8000
    assert budget.max_command_prompt_tokens == 2000


def test_small_context_scales_down_tool_budgets() -> None:
    budget = resolve_budget(ContextSettings(), detected_context_tokens=4096)
    assert budget.reserved_response_tokens == 1024  # clamped low
    assert budget.max_tool_prompt_tokens == 409  # 10% beats the 2000 cap
    assert budget.max_command_prompt_tokens == 409
    assert budget.max_user_message_tokens == 1024


def test_explicit_settings_win_over_detection() -> None:
    explicit = ContextSettings(model_context_tokens=16384, reserved_response_tokens=2222)
    budget = resolve_budget(explicit, detected_context_tokens=8192)
    assert budget.model_context_tokens == 16384
    assert budget.reserved_response_tokens == 2222


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_truncate_to_tokens() -> None:
    text, truncated = truncate_to_tokens("hello", 10)
    assert (text, truncated) == ("hello", False)
    long_text = "x" * 100
    bounded, truncated = truncate_to_tokens(long_text, 10)
    assert truncated
    assert bounded.startswith("x" * 40)
    assert "[truncated 60 chars]" in bounded
