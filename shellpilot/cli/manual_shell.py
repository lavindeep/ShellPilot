"""Manual Shell: raw shell=True commands typed by the user (design section 21).

The model never sees or routes these commands. Unlike the old implementation,
nothing here pretends raw shell is low risk: every command is audited as
raw_shell with its real exit status.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from shellpilot.persistence.audit_store import AuditLogger

BANNER = """Manual Shell
Commands run exactly as typed with shell=True.
The AI is not controlling this mode.
Type /exit-shell to return."""

PROMPT = "manual$ "
EXIT_COMMAND = "/exit-shell"


def run_manual_command(command: str, cwd: Path, audit: AuditLogger | None) -> int:
    """Run one user-typed command with shell=True, streaming to the terminal."""
    completed = subprocess.run(  # noqa: S602 - raw shell is this mode's explicit contract
        command, shell=True, cwd=cwd, check=False
    )
    if audit is not None:
        audit.write(
            "manual_shell_command",
            command=command,
            exit_code=completed.returncode,
            risk="raw_shell",
        )
    return completed.returncode


def manual_shell_loop(
    console: Console,
    cwd: Path,
    audit: AuditLogger | None,
    read_line: Callable[[], str] | None = None,
) -> None:
    """REPL for Manual Shell; returns when the user types /exit-shell."""
    console.print(f"[yellow]{escape(BANNER)}[/yellow]")
    if audit is not None:
        audit.write("manual_shell_enter")
    reader = read_line or (lambda: console.input(PROMPT))
    while True:
        try:
            line = reader().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == EXIT_COMMAND:
            break
        try:
            exit_code = run_manual_command(line, cwd, audit)
        except KeyboardInterrupt:
            # Ctrl+C aborts the running command, not the shell loop.
            console.print("[yellow]Interrupted.[/yellow]")
            continue
        if exit_code != 0:
            console.print(f"[dim]exit code {exit_code}[/dim]")
    if audit is not None:
        audit.write("manual_shell_exit")
    console.print("[dim]Left Manual Shell.[/dim]")
