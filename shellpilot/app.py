"""ShellPilot application entrypoint."""

from collections.abc import Sequence

from shellpilot.cli.commands import run_cli


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ShellPilot CLI."""
    return run_cli(argv)
