"""Prompt for model-driven memory optimization (design section 16.4)."""

from __future__ import annotations

MEMORY_COMPACT_PROMPT = """You maintain an assistant's stored behavior preferences.

Current entries (JSON):
{entries}

Optimize this list:
- Merge duplicate or overlapping preferences into one clear statement.
- Remove stale or session-specific entries.
- Never invent a preference that is not implied by an existing entry.
- Keep every entry whose source is "user"; you may tighten wording but never
  change its meaning or drop it.
- Prefer short, actionable statements.

Reply with ONLY a JSON array of the final entries, each reusing one existing
id: [{{"id": "pref_001", "text": "final text"}}]
"""
