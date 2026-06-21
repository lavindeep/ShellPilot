"""Tests for shellpilot.cli.banner — boot-banner renderer."""

from rich.console import Console
from rich.panel import Panel

from shellpilot.cli.banner import render_banner


def _export(panel: Panel, *, styles: bool = False) -> str:
    console = Console(record=True, width=100, force_terminal=True)
    console.print(panel)
    return console.export_text(styles=styles)


def test_renders_without_error_local() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False)
    assert isinstance(panel, Panel)
    text = _export(panel)
    assert "gemma4:e4b" in text


def test_renders_without_error_cloud() -> None:
    panel = render_banner("nemotron-3-nano:30b-cloud", is_cloud=True)
    assert isinstance(panel, Panel)
    text = _export(panel)
    assert "nemotron-3-nano:30b-cloud" in text


def test_welcome_text_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False)
    text = _export(panel)
    assert "Welcome back, pilot" in text


def test_jet_glyph_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False)
    text = _export(panel)
    assert "█" in text


def test_commands_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False)
    text = _export(panel)
    assert "/help" in text
    assert "/model" in text
    assert "! <cmd>" in text


def test_tip_present() -> None:
    panel = render_banner("gemma4:e4b", is_cloud=False)
    text = _export(panel)
    assert "high-risk commands" in text


def test_local_vs_cloud_different_color() -> None:
    local_panel = render_banner("testmodel", is_cloud=False)
    cloud_panel = render_banner("testmodel", is_cloud=True)
    local_styled = _export(local_panel, styles=True)
    cloud_styled = _export(cloud_panel, styles=True)
    # local uses green (#98c379), cloud uses amber (#e5c07b)
    # The styled export encodes ANSI color codes so we verify they differ
    assert local_styled != cloud_styled
