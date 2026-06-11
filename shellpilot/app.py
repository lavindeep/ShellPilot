"""ShellPilot application entrypoint."""

from collections.abc import Sequence

from shellpilot.cli.commands import run_cli
from shellpilot.tools.command import scrub_own_environment


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ShellPilot CLI."""
    scrub_own_environment()
    return run_cli(argv)
