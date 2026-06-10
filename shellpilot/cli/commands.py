"""Command-line argument parsing and dispatch."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from shellpilot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shellpilot",
        description="Local-first AI shell harness powered by Ollama.",
    )
    parser.add_argument("--version", action="version", version=f"shellpilot {__version__}")
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Workspace directory (defaults to the current directory).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor", help="Check local prerequisites (Python, Ollama, models, paths)."
    )
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = (args.cwd or Path.cwd()).resolve()
    if args.cwd is not None and not workspace.is_dir():
        print(f"error: workspace {workspace} does not exist", file=sys.stderr)
        return 2

    if args.command == "doctor":
        from shellpilot.cli.doctor import run_doctor

        return run_doctor(workspace)

    # Interactive conversation arrives with Phase 1.
    print(f"ShellPilot {__version__} — local-first AI shell harness")
    print("The conversation runtime is not implemented yet. See docs/DESIGN.md.")
    print(f"Workspace: {workspace}")
    print("Try: shellpilot doctor")
    return 0
