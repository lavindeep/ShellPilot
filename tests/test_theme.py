"""Tests for the terminal theme and glyph sets (design section 31)."""

from __future__ import annotations

import io
from dataclasses import replace
from typing import IO

from shellpilot.cli.theme import (
    ASCII_GLYPHS,
    SHELLPILOT_THEME,
    UNICODE_GLYPHS,
    build_console,
    resolve_glyphs,
)
from shellpilot.config.model import Settings

REQUIRED_STYLES = (
    "sp.emph",
    "sp.dim",
    "sp.faint",
    "sp.accent",
    "sp.success",
    "sp.warn",
    "sp.error",
    "sp.risk.high",
    "sp.badge.medium",
    "sp.badge.high",
    "sp.badge.blocked",
    "sp.diff.add",
    "sp.diff.remove",
    "sp.diff.add_word",
    "sp.diff.remove_word",
    "sp.diff.gutter",
    "sp.chevron",
)


class _EncodedFile(io.StringIO):
    """A writable text stream reporting a fixed encoding."""

    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


def test_theme_defines_all_required_styles() -> None:
    for name in REQUIRED_STYLES:
        assert name in SHELLPILOT_THEME.styles, f"missing theme style: {name}"


def test_build_console_resolves_theme_styles() -> None:
    console = build_console(Settings(), file=io.StringIO())
    style = console.get_style("sp.accent")
    assert style.color is not None


def test_build_console_respects_no_color_setting() -> None:
    settings = Settings()
    settings = replace(settings, ui=replace(settings.ui, no_color=True))
    console = build_console(settings, file=io.StringIO())
    assert console.no_color


def test_resolve_glyphs_explicit_modes() -> None:
    console = build_console(Settings(), file=io.StringIO())
    assert resolve_glyphs("unicode", console) is UNICODE_GLYPHS
    assert resolve_glyphs("ascii", console) is ASCII_GLYPHS


def test_resolve_glyphs_auto_picks_by_encoding() -> None:
    utf8_file: IO[str] = _EncodedFile("utf-8")
    ascii_file: IO[str] = _EncodedFile("ascii")
    assert resolve_glyphs("auto", build_console(Settings(), file=utf8_file)) is UNICODE_GLYPHS
    assert resolve_glyphs("auto", build_console(Settings(), file=ascii_file)) is ASCII_GLYPHS


def test_glyph_sets_cover_the_same_fields() -> None:
    assert UNICODE_GLYPHS.bullet != ASCII_GLYPHS.bullet
    assert UNICODE_GLYPHS.spinner_frames and ASCII_GLYPHS.spinner_frames
