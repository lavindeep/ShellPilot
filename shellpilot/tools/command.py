"""Agent command execution: argv with shell=False (design section 13.1).

Raw shell never runs here — pipelines, redirection, and expansions belong to
Manual Shell (section 13.2), where the user types the command themselves.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
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
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec

DEFAULT_TIMEOUT_SECONDS = 600
# Maximum chars read in a single readline() call so a newline-less stream cannot
# materialise an unbounded string before the total-capture cap is consulted.
MAX_READ_CHARS = 65_536
# Exit codes that mean "ran fine, found nothing" rather than failure (section 24.3).
EXPECTED_NONZERO: dict[str, frozenset[int]] = {
    "grep": frozenset({1}),
    "rg": frozenset({1}),
    "diff": frozenset({1}),
}

# Prefixes of loader/allocator debug-injection variables that must not leak into
# child processes.  Presence of MallocStackLogging (and friends) or DYLD_INSERT_*
# in the parent environment caused every child command to emit malloc diagnostics
# into stderr — merged into stdout — polluting both the UI and the model's context
# (the MallocStackLogging incident).
_STRIP_PREFIXES = ("DYLD_", "LD_", "Malloc")

# Non-interactive overrides: force headless children that would otherwise open a
# pager (less/more), launch an editor, or issue a credential prompt to behave
# deterministically instead of blocking on a tty until the 600 s timeout fires.
_NON_INTERACTIVE: dict[str, str] = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
}


def scrub_own_environment() -> None:
    """Remove loader/allocator debug-injection variables from this process.

    Children get a sanitized environment via subprocess_env(), but macOS
    libmalloc can still emit a diagnostic line from the fork window of every
    spawned command when ShellPilot's own environment carries Malloc* vars
    (the message is produced before exec, while the forked child still runs
    the parent's image).  Scrubbing our own environment at boot closes that
    gap.
    """
    for key in [k for k in os.environ if k.startswith(_STRIP_PREFIXES)]:
        del os.environ[key]


def subprocess_env() -> dict[str, str]:
    """Return a sanitized copy of the parent environment for child processes.

    Strips loader/allocator debug-injection variables (DYLD_*, LD_*, Malloc*)
    and forces non-interactive behavior for pagers, editors, and credential
    prompts.  PATH and all other variables pass through untouched so that
    activated-venv workflows continue to work.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(prefix) for prefix in _STRIP_PREFIXES)
    }
    env.update(_NON_INTERACTIVE)
    return env


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
    # NOTE (Investigation C, v0.5.1): the MallocStackLogging fork-window line cannot
    # be removed by switching to CPython's posix_spawn fast path. subprocess._execute_child
    # (Lib/subprocess.py) refuses the spawn branch unless cwd is None, start_new_session
    # is False, process_group == -1, AND close_fds is False (no POSIX_SPAWN_CLOSEFROM on
    # the macOS framework build). We require start_new_session=True for whole-process-group
    # SIGKILL on timeout (os.killpg below) and for Ctrl-C isolation: the only spawn-eligible
    # combo leaves children in ShellPilot's foreground process group, where a user Ctrl-C
    # reaches them (verified). We keep fork+setsid and accept the cosmetic chatter, which is
    # already minimized by scrub_own_environment().
    process = subprocess.Popen(  # noqa: S603 - shell=False argv execution is the design
        request.argv,
        cwd=request.cwd,
        env=request.env,
        stdin=subprocess.DEVNULL,
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
        while True:
            line = process.stdout.readline(MAX_READ_CHARS)
            if not line:
                break
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

# Tokens that ARE standalone shell operators when they appear anywhere in argv.
# NOTE: ";" is deliberately excluded — find -exec ... ; passes a literal ";"
# token as an argument to find, and a stray ";" elsewhere fails naturally
# without harm (the OS rejects the exec without any security concern).
_SHELL_OPERATOR_TOKENS = frozenset(
    {"|", "<", ">", ">>", "<<", "&", "&&", "||", "1>", "2>", "2>&1", "1>&2"}
)


def _packed_shell_line(argv: list[str]) -> bool:
    """Detect a whole shell command packed into one argv token (section 13.3).

    Small models sometimes send argv=["echo hi > f.txt"]; with shell=False that
    would exec a nonexistent binary, so reject with corrective guidance instead.
    """
    if len(argv) == 1 and " " in argv[0]:
        return True
    return any(marker in argv[0] for marker in _SHELL_SYNTAX_MARKERS)


def _resolve_executable(context: ToolContext, name: str) -> str | None:
    """Check whether argv[0] is launchable; return a failure message or None."""
    # Path-separator → treat as filesystem path, not a PATH lookup.
    if os.sep in name or "/" in name:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = context.workspace / candidate
        if not candidate.exists():
            return f"executable '{name}' not found"
        if not os.access(candidate, os.X_OK):
            return f"'{name}' is not executable"
        return None

    # Plain name: consult PATH.
    if shutil.which(name) is not None:
        return None

    msg = f"executable '{name}' not found on PATH"

    # Suggestions: try <name>3 first (covers python→python3, pip→pip3).
    if shutil.which(f"{name}3") is not None:
        return f"{msg} — did you mean: {name}3?"

    # Scan PATH for close matches via difflib.
    basenames: list[str] = []
    for dir_str in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in os.scandir(dir_str):
                if entry.is_file() and os.access(entry.path, os.X_OK):
                    basenames.append(entry.name)
        except OSError:
            pass
    suggestions = difflib.get_close_matches(name, sorted(set(basenames)), n=3, cutoff=0.8)
    if suggestions:
        return f"{msg} — did you mean: {', '.join(suggestions)}?"
    return msg


def _precheck_run_command(context: ToolContext, arguments: dict[str, Any]) -> str | None:
    """Pre-approval validation for run_command (section 13.3).

    Returns a failure message when the call can be rejected deterministically
    before classification/approval, or None to proceed.
    """
    argv = [str(token) for token in arguments.get("argv", [])]
    if not argv:
        return "argv must not be empty"
    if _packed_shell_line(argv):
        base_msg = (
            "argv must be separate tokens without shell syntax — "
            "this looks like a shell command packed into one string. run_command "
            "executes WITHOUT a shell: pass each argument as its own argv token, "
            'e.g. ["git", "status"]. Pipes, redirection (> <), and $() are not '
            "available — to write a file ask the user, and for shell-native "
            "syntax suggest Manual Shell (/shell)."
        )
        # Append a did-you-mean suggestion when the single token has no shell
        # syntax markers (i.e. it is a plain multi-word command, not a pipeline).
        if len(argv) == 1 and not any(marker in argv[0] for marker in _SHELL_SYNTAX_MARKERS):
            try:
                tokens = shlex.split(argv[0])
                if len(tokens) >= 2:
                    base_msg += f" Did you mean argv={json.dumps(tokens)}?"
            except ValueError:
                pass  # unbalanced quotes — skip suggestion
        return base_msg
    # Reject standalone shell-operator tokens anywhere in argv (not just argv[0]).
    for token in argv:
        if token in _SHELL_OPERATOR_TOKENS:
            return (
                f"argv token {token!r} is a shell operator — run_command executes "
                "WITHOUT a shell, so pipes and redirection cannot work. Run the "
                "command without it and process the output yourself, or suggest "
                "Manual Shell (/shell) for shell-native syntax."
            )
    return _resolve_executable(context, argv[0])


def _run_command(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    argv = [str(token) for token in arguments["argv"]]
    ceiling = context.command_timeout_seconds
    timeout = max(1, min(int(arguments.get("timeout_seconds", ceiling)), ceiling))
    classification = classify_command(argv, workspace=context.workspace)

    try:
        outcome = run_command_process(
            CommandRequest(
                argv=argv,
                cwd=context.workspace,
                timeout_seconds=timeout,
                env=subprocess_env(),
            ),
            max_capture_chars=context.max_capture_chars,
            emit_line=context.emit_output,
        )
    except OSError as exc:
        return ToolResult(success=False, summary=f"could not start command: {exc}", content="")

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
    precheck=_precheck_run_command,
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
                "minimum": 1,
                "description": (
                    "Optional timeout in seconds. Clamped to the configured "
                    "maximum (default 600); floor 1. The model may request a "
                    "shorter value but never exceed the ceiling."
                ),
            },
        },
        required=("argv",),
    ),
    side_effect=SideEffect.VARIABLE,
    default_risk=RiskLevel.MEDIUM,
    allowed_profiles=ALL_PROFILES,
    handler=_run_command,
)
