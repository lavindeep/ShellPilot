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
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="ID",
        help="Resume a saved session (latest for this workspace, or a session id).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor", help="Check local prerequisites (Python, Ollama, models, paths)."
    )
    config_parser = subparsers.add_parser("config", help="Inspect or edit configuration.")
    config_parser.add_argument("action", choices=["show", "edit"])
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

    if args.command == "config":
        return _run_config(workspace, args.action)

    from shellpilot.cli.terminal import run_interactive

    return run_interactive(workspace, resume=args.resume)


def _run_config(workspace: Path, action: str) -> int:
    import os

    from rich.console import Console

    from shellpilot.cli.slash import render_config
    from shellpilot.cli.terminal import config_files
    from shellpilot.config.loader import ConfigError, load_config
    from shellpilot.persistence.paths import AppPaths

    console = Console()
    env = dict(os.environ)
    user_file, project_file = config_files(workspace, env, AppPaths.default())
    if action == "edit":
        console.print(f"User config: {user_file}")
        console.print(f"Project config: {project_file}")
        return 0
    try:
        loaded = load_config(user_config_file=user_file, project_config_file=project_file, env=env)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        return 2
    render_config(loaded, console)
    return 0
