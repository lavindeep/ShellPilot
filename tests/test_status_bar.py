"""Tests for the persistent bottom status bar builder (design section 31/32)."""

from __future__ import annotations

from pathlib import Path

from shellpilot.cli.status_bar import (
    COLOR_ACCENT,
    COLOR_ERROR,
    COLOR_WARN,
    ctx_percent,
    status_bar,
)


def _plain(fragments: list[tuple[str, str]]) -> str:
    """Concatenate the visible text of a prompt_toolkit FormattedText list."""
    return "".join(text for _style, text in fragments)


def _styles_for(fragments: list[tuple[str, str]], needle: str) -> str:
    """The style string of the first fragment whose text contains *needle*."""
    for style, text in fragments:
        if needle in text:
            return style
    raise AssertionError(f"no fragment containing {needle!r}")


def test_ctx_percent_thresholds() -> None:
    # Pure %: rounded, clamped to [0, 100]; zero total never divides.
    assert ctx_percent(0, 1000) == 0
    assert ctx_percent(120, 1000) == 12
    assert ctx_percent(1000, 1000) == 100
    assert ctx_percent(2000, 1000) == 100  # clamps over budget
    assert ctx_percent(50, 0) == 0  # no budget → no division


def test_local_session_is_green_and_local() -> None:
    bar = status_bar(
        workspace=Path.home() / "Projects",
        model="gemma4:e4b",
        profile="balanced",
        is_cloud=False,
        ctx_pct=12,
    )
    text = _plain(bar)
    # Home-abbreviated workspace, model, profile, locality, ctx all present.
    assert "~/Projects" in text
    assert "gemma4:e4b" in text
    assert "balanced" in text
    assert "local" in text
    assert "12%" in text
    assert "ctx" in text
    # Local: NO amber anywhere — the cloud emphasis must be unmistakably absent.
    assert COLOR_WARN not in "".join(style for style, _ in bar)
    # Model name is GREEN when local.
    assert COLOR_ACCENT in _styles_for(bar, "gemma4:e4b")
    # The local locality glyph (filled dot) is green.
    assert "●" in text
    assert COLOR_ACCENT in _styles_for(bar, "●")


def test_cloud_session_is_amber_and_carries_emphasis() -> None:
    bar = status_bar(
        workspace=Path.home() / "Projects",
        model="gemma4:31b-cloud",
        profile="balanced",
        is_cloud=True,
        ctx_pct=41,
    )
    text = _plain(bar)
    assert "gemma4:31b-cloud" in text
    # Unmistakable cloud indicator: the cloud glyph + CLOUD label.
    assert "☁" in text
    assert "CLOUD" in text
    # Model name is AMBER when egressing.
    assert COLOR_WARN in _styles_for(bar, "gemma4:31b-cloud")
    # Locality segment is amber + bold.
    cloud_style = _styles_for(bar, "CLOUD")
    assert COLOR_WARN in cloud_style
    assert "bold" in cloud_style
    # The bar carries an amber emphasis even on a non-locality fragment (the
    # "wash" adapted to a terminal: amber-tinted separators), distinguishing it
    # from a local bar at a glance.
    assert any(COLOR_WARN in style and "●" not in textfrag for style, textfrag in bar)


def test_ctx_color_thresholds_lo_mid_hi() -> None:
    lo = status_bar(workspace=Path.home(), model="m", profile="p", is_cloud=False, ctx_pct=20)
    mid = status_bar(workspace=Path.home(), model="m", profile="p", is_cloud=False, ctx_pct=65)
    hi = status_bar(workspace=Path.home(), model="m", profile="p", is_cloud=False, ctx_pct=92)
    assert COLOR_ACCENT in _styles_for(lo, "20%")
    assert COLOR_WARN in _styles_for(mid, "65%")
    assert COLOR_ERROR in _styles_for(hi, "92%")


def test_separators_present_between_left_segments() -> None:
    bar = status_bar(
        workspace=Path.home() / "Projects",
        model="gemma4:e4b",
        profile="balanced",
        is_cloud=False,
        ctx_pct=12,
    )
    text = _plain(bar)
    # Faint dot separators join the left segments (dir · model · profile · loc).
    assert text.count("·") >= 3


def test_workspace_home_is_abbreviated() -> None:
    bar = status_bar(
        workspace=Path.home(),
        model="m",
        profile="p",
        is_cloud=False,
        ctx_pct=0,
    )
    text = _plain(bar)
    assert "~" in text
    # The literal home path must not leak verbatim.
    assert str(Path.home()) not in text


def test_control_chars_in_workspace_are_sanitized() -> None:
    # A workspace path is user-controlled and could carry odd bytes; the bar must
    # strip control/ANSI before render so it cannot repaint the terminal.
    nasty = Path("/tmp/\x1b[31mhack\x07\x00bad")
    bar = status_bar(
        workspace=nasty,
        model="m",
        profile="p",
        is_cloud=False,
        ctx_pct=0,
    )
    text = _plain(bar)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "\x00" not in text
