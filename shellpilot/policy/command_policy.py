"""Deterministic command risk classification (design section 14.3).

Policy is deterministic first: no model call ever decides risk, and the model
can never downgrade what this module returns (section 14.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from shellpilot.policy.risk import RiskLevel

LOW_EXECUTABLES: Final = frozenset(
    {
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "wc",
        "which",
        "file",
        "stat",
        "du",
        "df",
        "uname",
        "date",
        "whoami",
        "echo",
        "tree",
        "grep",
        "rg",
        "fgrep",
        "egrep",
        "true",
        "false",
        "ps",
    }
)
READER_EXECUTABLES: Final = frozenset(
    {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "file", "stat", "du"}
)
GIT_READONLY_VERBS: Final = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "blame",
        "shortlog",
        "describe",
        "remote",
        "rev-parse",
        "ls-files",
        "stash",
    }
)
GIT_HIGH: Final = frozenset({"reset", "clean"})
GIT_BENIGN_GLOBALS: Final = frozenset({"--no-pager", "--literal-pathspecs"})
GIT_TERMINAL_GLOBALS: Final = frozenset({"--exec-path"})
GIT_GLOBALS_WITH_SPLIT_VALUES: Final = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)
SHELLS: Final = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh"})
PACKAGE_MANAGERS: Final = frozenset(
    {
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "brew",
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "cargo",
        "gem",
        "uv",
    }
)
NETWORK_COMMANDS: Final = frozenset({"curl", "wget", "nc", "ssh", "scp", "rsync", "ftp"})
WRITE_COMMANDS: Final = frozenset({"mv", "cp", "mkdir", "touch", "ln", "tee", "patch"})
HIGH_COMMANDS: Final = frozenset(
    {
        "sudo",
        "doas",
        "killall",
        "dd",
        "mkfs",
        "diskutil",
        "shutdown",
        "reboot",
        "launchctl",
        "systemctl",
    }
)
SECRET_MARKERS: Final = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
)


@dataclass(frozen=True)
class CommandRisk:
    """Deterministic classification with human-readable reasons."""

    risk: RiskLevel
    reasons: tuple[str, ...]


def _touches_secret_path(argv: list[str]) -> str | None:
    for token in argv[1:]:
        lowered = token.lower()
        for marker in SECRET_MARKERS:
            if marker in lowered:
                return f"argument {token!r} looks like a credential/secret path"
    return None


def sensitive_path_reason(path: Path) -> str | None:
    """Reason when any path *component* names a credential/secret, else None.

    Component-exact matching (not the substring rule of `_touches_secret_path`):
    a component matches a marker when it equals the marker exactly, or — for the
    dotfile markers (those starting with ``.``) — when it starts with
    ``marker + "."``. That dot rule covers ``.env.local`` / ``.env.production``
    while leaving plain-name markers exact, so ``environment.py``,
    ``secrets_test.py`` and ``credentials.md.bak`` do not match. Shares
    SECRET_MARKERS as the single source of truth.
    """
    for component in path.parts:
        lowered = component.lower()
        for marker in SECRET_MARKERS:
            if lowered == marker:
                return f"reads a sensitive path ({component})"
            if marker.startswith(".") and lowered.startswith(marker + "."):
                return f"reads a sensitive path ({component})"
    return None


def _path_arg_outside_workspace(argv: list[str], workspace: Path) -> str | None:
    """Flag path arguments that resolve outside the workspace (reads or writes).

    Both absolute and relative tokens are checked. Bare non-path tokens (e.g.
    "git", "status", a commit message) resolve to workspace/<token>, which is
    inside the workspace, so they produce no false positives. A token like ".."
    or "../foo" resolves outside and is correctly flagged.
    """
    root = workspace.resolve()
    for token in argv[1:]:
        if token.startswith("-"):
            # A flag may still hide a path in its value (e.g. --file=/etc/passwd,
            # --output=/etc/x). Check the substring after the first '=' when it
            # looks path-like; otherwise the flag carries no path to check.
            if "=" not in token:
                continue
            value = token.split("=", 1)[1]
            if "/" not in value and not value.startswith(".."):
                continue
            candidate = value
        else:
            candidate = token
        try:
            if candidate.startswith("/"):
                target = Path(candidate).resolve()
            else:
                target = (workspace / candidate).resolve()
        except OSError:
            continue
        if root != target and root not in target.parents:
            return f"target {candidate} is outside the workspace boundary"
    return None


def _scan_git_verb(argv: list[str]) -> tuple[str, list[str], bool]:
    conservative_global = False
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            return token, argv[index + 1 :], conservative_global
        if token not in GIT_BENIGN_GLOBALS:
            conservative_global = True
        if token in GIT_TERMINAL_GLOBALS:
            return "", [], conservative_global
        if token in GIT_GLOBALS_WITH_SPLIT_VALUES:
            index += 2
        else:
            index += 1
    return "", [], conservative_global


def _is_git_branch_delete_flag(flag: str) -> bool:
    if flag.startswith("--"):
        option = flag.partition("=")[0]
        return len(option) >= len("--dele") and "--delete".startswith(option)
    return flag.startswith("-") and any(option in "dD" for option in flag[1:])


def _is_git_force_with_lease_flag(flag: str) -> bool:
    option = flag.partition("=")[0]
    return len(option) >= len("--force-w") and "--force-with-lease".startswith(option)


def _is_git_push_short_force_flag(flag: str) -> bool:
    if flag.startswith("--"):
        return False
    for option in flag[1:]:
        if option == "f":
            return True
        if option == "o":
            return False
    return False


GIT_BRANCH_READONLY_FLAGS: Final = frozenset(
    {"-l", "--list", "-a", "--all", "-r", "--remotes", "--contains", "--merged", "--points-at"}
)


def _git_output_path(tokens: list[str]) -> str | None:
    """The path value of a ``--output`` (or ``-O`` order-file) option, else None.

    ``--output`` is a global diff-formatting option honoured by every
    diff-emitting verb (``diff``/``show``/``log``/``stash show``/…), so the whole
    invocation is scanned rather than a single verb. ``-O`` is git's order-file
    option, not an ``--output`` short form; it is covered conservatively (an
    out-of-workspace order-file is still a boundary crossing worth escalating).
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--output", "-O"):
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith("--output="):
            return token[len("--output=") :]
        if token.startswith("-O") and len(token) > 2:
            return token[2:]
        index += 1
    return None


def _classify_git(argv: list[str], workspace: Path) -> CommandRisk:
    verb, verb_args, conservative_global = _scan_git_verb(argv)
    flags = [token for token in argv[1:] if token.startswith("-")]
    if verb in GIT_HIGH:
        return CommandRisk(RiskLevel.HIGH, (f"git {verb} can destroy local changes",))
    if verb == "branch" and any(_is_git_branch_delete_flag(flag) for flag in flags):
        return CommandRisk(RiskLevel.HIGH, ("git branch deletion",))
    if verb == "push":
        if (
            "--force" in flags
            or any(_is_git_push_short_force_flag(flag) for flag in flags)
            or any(_is_git_force_with_lease_flag(flag) for flag in flags)
            or any(arg.startswith("+") for arg in verb_args)
        ):
            return CommandRisk(RiskLevel.HIGH, ("force push rewrites remote history",))
        return CommandRisk(RiskLevel.MEDIUM, ("git push publishes commits",))
    # --output writes an arbitrary file from any diff-emitting verb, so scan the
    # whole invocation. Placed after every HIGH determination above and before
    # any read-only LOW return so it only ever escalates a would-be-LOW command
    # (never downgrades an already-HIGH one, e.g. `branch -D x --output=in_ws`).
    output_path = _git_output_path(argv[1:])
    if output_path is not None:
        if _path_arg_outside_workspace(["git", output_path], workspace):
            return CommandRisk(
                RiskLevel.HIGH,
                (f"git {verb or '?'} output path is outside the workspace boundary",),
            )
        return CommandRisk(RiskLevel.MEDIUM, (f"git {verb or '?'} writes output to a file",))
    if verb in ("branch", "remote"):
        # A non-flag positional names a branch/remote to create/rename/add/set —
        # a state mutation, not a read-only listing (which carries no name or a
        # listing flag like --list/-a). Bare `git branch`/`git remote -v` stay LOW.
        listing = verb == "branch" and any(flag in GIT_BRANCH_READONLY_FLAGS for flag in flags)
        if not listing and any(not arg.startswith("-") for arg in verb_args):
            return CommandRisk(RiskLevel.MEDIUM, (f"git {verb} changes repository state",))
    if conservative_global:
        return CommandRisk(RiskLevel.MEDIUM, ("git uses a non-benign global option",))
    if verb in GIT_READONLY_VERBS and verb != "stash":
        return CommandRisk(RiskLevel.LOW, ())
    if verb == "stash" and verb_args and verb_args[0] in ("list", "show"):
        return CommandRisk(RiskLevel.LOW, ())
    return CommandRisk(RiskLevel.MEDIUM, (f"git {verb or '?'} changes repository state",))


def _classify_rm(argv: list[str], workspace: Path) -> CommandRisk:
    flags = "".join(token.lstrip("-") for token in argv[1:] if token.startswith("-"))
    if "r" in flags or "R" in flags:
        return CommandRisk(RiskLevel.HIGH, ("recursive delete",))
    if any("*" in token for token in argv[1:]):
        return CommandRisk(RiskLevel.HIGH, ("glob delete",))
    outside = _path_arg_outside_workspace(argv, workspace)
    if outside:
        return CommandRisk(RiskLevel.HIGH, ("deletes outside the workspace",))
    return CommandRisk(RiskLevel.MEDIUM, ("deletes a file",))


def classify_command(argv: list[str], *, workspace: Path) -> CommandRisk:
    """Classify an argv command (shell=False) by deterministic rules."""
    if not argv or not argv[0].strip():
        return CommandRisk(RiskLevel.BLOCKED, ("empty command",))

    executable = Path(argv[0]).name
    if argv[0] != executable:
        risk = classify_command([executable, *argv[1:]], workspace=workspace)
        if risk.risk == RiskLevel.LOW:
            return CommandRisk(RiskLevel.MEDIUM, ("path-qualified executable",))
        return risk

    secret = _touches_secret_path(argv)

    if executable in HIGH_COMMANDS:
        return CommandRisk(RiskLevel.HIGH, (f"{executable} is privileged or system-destructive",))
    if executable in SHELLS:
        return CommandRisk(
            RiskLevel.HIGH,
            (f"{executable} runs raw shell syntax; use Manual Shell instead",),
        )
    if executable == "rm":
        return _classify_rm(argv, workspace)
    if executable == "git":
        risk = _classify_git(argv, workspace)
        if secret:
            return CommandRisk(RiskLevel.HIGH, (*risk.reasons, secret))
        return risk
    if executable == "chmod" or executable == "chown":
        if any(token in ("-R", "--recursive") for token in argv[1:]):
            return CommandRisk(RiskLevel.HIGH, ("recursive permission change",))
        return CommandRisk(RiskLevel.MEDIUM, ("permission change",))
    if executable == "find" and any(token in ("-delete", "-exec") for token in argv[1:]):
        return CommandRisk(RiskLevel.HIGH, ("find with -delete/-exec can modify files",))

    if secret:
        return CommandRisk(RiskLevel.HIGH, (secret,))

    if executable in PACKAGE_MANAGERS:
        return CommandRisk(RiskLevel.MEDIUM, (f"{executable} package operation",))
    if executable in NETWORK_COMMANDS:
        return CommandRisk(RiskLevel.MEDIUM, (f"{executable} performs network activity",))
    if executable in WRITE_COMMANDS:
        outside = _path_arg_outside_workspace(argv, workspace)
        if outside:
            return CommandRisk(RiskLevel.HIGH, (outside,))
        return CommandRisk(RiskLevel.MEDIUM, (f"{executable} writes to the workspace",))
    if executable == "kill":
        return CommandRisk(RiskLevel.MEDIUM, ("signals a process",))
    if executable in ("python", "python3"):
        if argv[1:] == ["--version"]:
            return CommandRisk(RiskLevel.LOW, ())
        return CommandRisk(RiskLevel.MEDIUM, ("runs arbitrary python code",))
    if executable in READER_EXECUTABLES:
        # Unlike read_file (which honors allow_sensitive_reads via decide()),
        # classify_command sees only a RiskLevel and run_command is
        # SideEffect.VARIABLE, so out-of-workspace command reads escalate to
        # HIGH unconditionally and never AUTO-run. A regex/pattern arg that
        # looks path-like (e.g. grep "../x") can over-flag to HIGH; that is a
        # safe over-ask (HIGH -> ASK), shared with the rm/WRITE branches.
        outside = _path_arg_outside_workspace(argv, workspace)
        if outside:
            return CommandRisk(
                RiskLevel.HIGH, (f"reads outside the workspace boundary: {outside}",)
            )
        # in-workspace readers fall through to the LOW return below
    if executable in LOW_EXECUTABLES:
        return CommandRisk(RiskLevel.LOW, ())

    return CommandRisk(
        RiskLevel.MEDIUM, (f"unknown executable {executable!r}; defaulting to medium",)
    )
