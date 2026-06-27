"""Terminal theme and glyph sets for the v2 visual design (design section 31).

All named styles live here; no other module hard-codes colors. "Instrument
minimal": monochrome hierarchy on the user's terminal background, with color
only where it carries meaning (accent green, risk red, warning amber).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO

from rich.console import Console
from rich.theme import Theme

from shellpilot.config.model import Settings

# Single-source hex values for the four shared theme colors.  Other modules
# (banner.py, status_bar.py) import these constants so the values can't drift.
COLOR_ACCENT = "#98c379"
COLOR_WARN = "#e5c07b"
COLOR_DIM = "#6b6b6b"
COLOR_FAINT = "#444444"

SHELLPILOT_THEME = Theme(
    {
        "sp.emph": "bold bright_white",
        "sp.dim": COLOR_DIM,
        "sp.faint": COLOR_FAINT,
        "sp.accent": COLOR_ACCENT,
        "sp.success": COLOR_ACCENT,
        "sp.warn": COLOR_WARN,
        "sp.error": "#e06c75",
        "sp.risk.high": "bold #e06c75",
        "sp.badge.medium": "bold white on #3a3a3a",
        "sp.badge.high": "bold white on #c14949",
        "sp.badge.blocked": "bold black on #e5c07b",
        "sp.diff.add": "#98c379 on #20281c",
        "sp.diff.remove": "#e06c75 on #2e2123",
        "sp.diff.add_word": "#dfffd0 on #3f5230",
        "sp.diff.remove_word": "#ffd7d7 on #5c3338",
        "sp.diff.gutter": "#6b6b6b",
        "sp.chevron": "bold #98c379",
    }
)


@dataclass(frozen=True)
class Glyphs:
    """Display characters; the ASCII set is the degradation target (section 31.9)."""

    bullet: str
    elbow: str
    chevron: str
    todo: str
    current: str
    skip: str
    check: str
    cross: str
    ellipsis: str
    spinner_frames: tuple[str, ...]
    beacon_frames: tuple[str, ...]


UNICODE_GLYPHS = Glyphs(
    bullet="⏺",
    elbow="⎿",
    chevron="❯",
    todo="☐",
    current="▶",
    skip="·",
    check="✓",
    cross="✗",
    ellipsis="…",
    # Compact-glide plane across a 4-cell track; U+2708 text presentation (no VS16).
    spinner_frames=("✈···", "·✈··", "··✈·", "···✈", "····"),
    # Breathing-pulse beacon for labeled states; each step held 2 ticks.
    beacon_frames=("·", "·", "✧", "✧", "✦", "✦", "✧", "✧"),
)

ASCII_GLYPHS = Glyphs(
    bullet="*",
    elbow="`-",
    chevron=">",
    todo="[ ]",
    current="[>]",
    skip="-",
    check="+",
    cross="x",
    ellipsis="...",
    spinner_frames=(">---", "->--", "-->-", "--->", "----"),
    beacon_frames=(".", ".", "+", "+", "*", "*", "+", "+"),
)

# _PROBE must contain every unicode glyph the UI can emit so auto-detection works.
# Includes spinner (✈) and beacon (✦ ✧) glyphs; ◐ removed (no longer used).
_PROBE = "".join(("⏺", "⎿", "❯", "✈", "☐", "✓", "▶", "✗", "…", "╭", "✦", "✧"))


def build_console(settings: Settings, file: IO[str] | None = None) -> Console:
    """Themed console; rich handles NO_COLOR, TTY detection, and downgrades."""
    return Console(
        theme=SHELLPILOT_THEME,
        no_color=True if settings.ui.no_color else None,
        file=file,
    )


def resolve_glyphs(mode: str, console: Console) -> Glyphs:
    """Pick the glyph set for `ui.glyphs`; "auto" probes the output encoding."""
    if mode == "unicode":
        return UNICODE_GLYPHS
    if mode == "ascii":
        return ASCII_GLYPHS
    if console.legacy_windows:
        return ASCII_GLYPHS
    encoding = getattr(console.file, "encoding", None) or "ascii"
    try:
        _PROBE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ASCII_GLYPHS
    return UNICODE_GLYPHS
