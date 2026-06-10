"""ShellPilot application entrypoint."""

from shellpilot import __version__


def main() -> int:
    """Run the ShellPilot CLI."""
    print(f"ShellPilot {__version__} — local-first AI shell harness")
    print("The conversation runtime is not implemented yet. See docs/DESIGN.md.")
    return 0
