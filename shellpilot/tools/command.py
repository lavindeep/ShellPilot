"""Agent command execution: argv with shell=False (design section 13.1).

Raw shell never runs here — pipelines, redirection, and expansions belong to
Manual Shell (section 13.2), where the user types the command themselves.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.command_policy import CommandRisk, classify_command
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ToolContext, ToolResult, ToolSpec
from shellpilot.tools.filesystem import ALL_PROFILES

DEFAULT_TIMEOUT_SECONDS = 600
# Exit codes that mean "ran fine, found nothing" rather than failure (section 24.3).
EXPECTED_NONZERO: dict[str, frozenset[int]] = {
    "grep": frozenset({1}),
    "rg": frozenset({1}),
    "diff": frozenset({1}),
}


@dataclass(frozen=True)
class CommandRequest:
    argv: list[str]
    cwd: Path
    timeout_seconds: int
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None
    output: str
    timed_out: bool
    truncated: bool


def run_command_process(
    request: CommandRequest,
    *,
    max_capture_chars: int,
    emit_line: Callable[[str], None] | None = None,
) -> CommandOutcome:
    """Run argv with shell=False, streaming output, bounding capture, killing on timeout."""
    process = subprocess.Popen(  # noqa: S603 - shell=False argv execution is the design
        request.argv,
        cwd=request.cwd,
        env=request.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
        start_new_session=True,
    )

    captured: list[str] = []
    captured_chars = 0
    truncated = False

    def reader() -> None:
        nonlocal captured_chars, truncated
        assert process.stdout is not None
        for line in process.stdout:
            if emit_line is not None:
                emit_line(line.rstrip("\n"))
            if captured_chars < max_capture_chars:
                captured.append(line)
                captured_chars += len(line)
            else:
                truncated = True

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    timed_out = False
    try:
        process.wait(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=5)
    thread.join(timeout=5)

    return CommandOutcome(
        exit_code=process.returncode,
        output="".join(captured),
        timed_out=timed_out,
        truncated=truncated,
    )


def _success(argv: list[str], outcome: CommandOutcome) -> bool:
    if outcome.timed_out or outcome.exit_code is None:
        return False
    if outcome.exit_code == 0:
        return True
    executable = Path(argv[0]).name
    return outcome.exit_code in EXPECTED_NONZERO.get(executable, frozenset())


_SHELL_SYNTAX_MARKERS = (">", "<", "|", ";", "&&", "||", "$(", "`")


def _packed_shell_line(argv: list[str]) -> bool:
    """Detect a whole shell command packed into one argv token (section 13.3).

    Small models sometimes send argv=["echo hi > f.txt"]; with shell=False that
    would exec a nonexistent binary, so reject with corrective guidance instead.
    """
    if len(argv) == 1 and " " in argv[0]:
        return True
    return any(marker in argv[0] for marker in _SHELL_SYNTAX_MARKERS)


def _run_command(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    argv = [str(token) for token in arguments["argv"]]
    if not argv:
        return ToolResult(success=False, summary="argv must not be empty", content="")
    if _packed_shell_line(argv):
        return ToolResult(
            success=False,
            summary="argv must be separate tokens without shell syntax",
            content=(
                "This looks like a shell command packed into one string. run_command "
                "executes WITHOUT a shell: pass each argument as its own argv token, "
                'e.g. ["git", "status"]. Pipes, redirection (> <), and $() are not '
                "available — to write a file ask the user, and for shell-native "
                "syntax suggest Manual Shell (/shell)."
            ),
        )
    timeout = int(arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    classification = classify_command(argv, workspace=context.workspace)

    outcome = run_command_process(
        CommandRequest(argv=argv, cwd=context.workspace, timeout_seconds=timeout),
        max_capture_chars=context.max_capture_chars,
        emit_line=context.emit_output,
    )
    if outcome.timed_out:
        summary = f"timed out after {timeout}s; process group killed"
    else:
        summary = f"exit code {outcome.exit_code}"
    return ToolResult(
        success=_success(argv, outcome),
        summary=summary,
        content=outcome.output,
        truncated=outcome.truncated,
        risk=classification.risk,
        side_effect=SideEffect.VARIABLE,
        metadata={"argv": " ".join(argv), "exit_code": str(outcome.exit_code)},
    )


def _classify(context: ToolContext, arguments: dict[str, Any]) -> CommandRisk:
    argv = [str(token) for token in arguments.get("argv", [])]
    return classify_command(argv, workspace=context.workspace)


RUN_COMMAND = ToolSpec(
    classifier=_classify,
    definition=ToolDefinition(
        name="run_command",
        description=(
            "Run a command in the workspace WITHOUT a shell, as an argv list "
            '(e.g. ["pytest", "-q"]). No pipes, redirection, globs, or env '
            "expansion — ask the user to use Manual Shell for those."
        ),
        parameters={
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command and arguments as separate strings.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional timeout; default 600.",
            },
        },
        required=("argv",),
    ),
    side_effect=SideEffect.VARIABLE,
    default_risk=RiskLevel.MEDIUM,
    allowed_profiles=ALL_PROFILES,
    handler=_run_command,
)
