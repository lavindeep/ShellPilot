"""Tests for shellpilot.cli.banner — boot-banner renderer."""

from rich.console import Console
from rich.panel import Panel

from shellpilot.cli.banner import render_banner

_JET_GLYPHS = ("▀", "▄", "█")


def _export(panel: Panel, *, styles: bool = False, width: int = 120) -> str:
    console = Console(record=True, width=width, force_terminal=True)
    console.print(panel)
    return console.export_text(styles=styles)


def _jet_left_cells(text: str) -> list[str]:
    """Left-column slice of each jet row, trailing space KEPT.

    Trailing whitespace is load-bearing for the symmetry check: the jet block
    occupies a fixed-width field, and a centered row's right margin lives in
    that trailing space. We slice between the panel's left border `│` and the
    interior divider `│`, keeping everything (including spaces) in between.
    """
    rows: list[str] = []
    for line in text.splitlines():
        if not any(g in line for g in _JET_GLYPHS):
            continue
        first = line.index("│")  # left panel border
        divider = line.index("│", first + 1)  # interior column divider
        rows.append(line[first + 1 : divider])
    return rows


def test_renders_without_error_local() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False, profile="balanced")
    assert isinstance(panel, Panel)
    text = _export(panel)
    assert "gemma4:e4b" in text


def test_renders_without_error_cloud() -> None:
    panel = render_banner("nemotron-3-nano:30b-cloud", is_cloud=True, profile="balanced")
    assert isinstance(panel, Panel)
    text = _export(panel)
    assert "nemotron-3-nano:30b-cloud" in text


def test_welcome_text_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False, profile="balanced")
    text = _export(panel)
    assert "Welcome back, pilot" in text


def test_jet_glyph_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False, profile="balanced")
    text = _export(panel)
    assert "█" in text


def test_subline_shows_profile_and_locality() -> None:
    local = _export(render_banner("m", is_cloud=False, profile="balanced"))
    assert "balanced · local" in local
    cloud = _export(render_banner("m", is_cloud=True, profile="trusted"))
    assert "trusted · cloud" in cloud


def test_command_section_present() -> None:
    text = _export(render_banner("m", is_cloud=False, profile="balanced"))
    assert "Commands" in text
    assert "/help" in text
    assert "/plan" in text
    assert "/skills" in text
    assert "/status" in text


def test_tips_section_present() -> None:
    text = _export(render_banner("m", is_cloud=False, profile="balanced"))
    assert "Tips" in text
    assert "for slash commands" in text
    assert "to run a shell command" in text
    assert "to confirm a high-risk command" in text


def test_workflow_skills_section_enabled() -> None:
    text = _export(
        render_banner("m", is_cloud=False, profile="balanced", skills=("debugging", "verification"))
    )
    assert "Workflow skills" in text
    assert "debugging" in text
    assert "verification" in text
    # No enable hint when skills are already enabled.
    assert "/skills to enable" not in text


def test_workflow_skills_section_hint_when_none_enabled() -> None:
    text = _export(render_banner("m", is_cloud=False, profile="balanced", skills=()))
    assert "Workflow skills" in text
    assert "/skills to enable" in text


def test_recent_sessions_section_present_when_nonempty() -> None:
    text = _export(
        render_banner(
            "m",
            is_cloud=False,
            profile="balanced",
            recent_sessions=(("docs-pass", "39m ago"), ("try-auth-fix", "2h ago")),
        )
    )
    assert "Recent sessions" in text
    assert "docs-pass" in text
    assert "39m ago" in text
    assert "try-auth-fix" in text


def test_recent_sessions_section_omitted_when_empty() -> None:
    text = _export(render_banner("m", is_cloud=False, profile="balanced", recent_sessions=()))
    assert "Recent sessions" not in text


def test_version_title_present() -> None:
    from shellpilot import __version__

    text = _export(render_banner("m", is_cloud=False, profile="balanced"))
    assert f"ShellPilot v{__version__}" in text


def test_local_vs_cloud_different_color() -> None:
    local_styled = _export(
        render_banner("testmodel", is_cloud=False, profile="balanced"), styles=True
    )
    cloud_styled = _export(
        render_banner("testmodel", is_cloud=True, profile="balanced"), styles=True
    )
    # local uses green (#98c379), cloud uses amber (#e5c07b); styled export
    # encodes ANSI color codes so we verify they differ.
    assert local_styled != cloud_styled


def test_jet_is_left_right_symmetric() -> None:
    """Regression: the jet must render symmetric (equal leading/trailing margin).

    The earlier per-line ``justify="center"`` implementation drifted because
    Rich strips trailing whitespace per line, so narrower jet rows lost their
    right margin and slid off-center. Each jet row must occupy a fixed-width
    field with equal leading and trailing margin around the glyph span.
    """
    text = _export(render_banner("gemma4:e4b", is_cloud=False, profile="balanced"))
    rows = _jet_left_cells(text)
    assert rows, "no jet rows found in render"
    # The jet block is centered as a unit: every row shares the same fixed-width
    # field, so the field's left edge is the minimum leading whitespace and its
    # right edge the minimum trailing whitespace across all rows.
    field_left = min(len(r) - len(r.lstrip(" ")) for r in rows)
    field_right = min(len(r) - len(r.rstrip(" ")) for r in rows)
    for raw in rows:
        # Glyph span within the shared field.
        first = min(raw.index(g) for g in _JET_GLYPHS if g in raw)
        last = max(raw.rindex(g) for g in _JET_GLYPHS if g in raw)
        leading = first - field_left
        trailing = (len(raw) - field_right) - 1 - last
        assert leading == trailing, (
            f"jet row not centered: leading={leading} trailing={trailing} :: {raw!r}"
        )


def test_recent_session_label_sanitized() -> None:
    """A control/ANSI sequence in a recent-session label is stripped (Group B).

    The label is a snippet of a past session's first user message — untrusted,
    possibly-pasted input. A stored escape (e.g. clear-screen) must not repaint
    the terminal at boot.
    """
    panel = render_banner(
        "gemma4:e4b",
        is_cloud=False,
        profile="balanced",
        recent_sessions=[("hi\x1b[2Jpwned", "1h ago")],
    )
    out = _export(panel)  # plain text; the label's ESC is the only escape source
    assert "\x1b" not in out, "raw ESC from the session label must be stripped before render"
    assert "pwned" in out, "the visible label text should survive, minus the escape byte"
