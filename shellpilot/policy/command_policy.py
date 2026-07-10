"""Deterministic command risk classification (design section 14.3).

Policy is deterministic first: no model call ever decides risk, and the model
can never downgrade what this module returns (section 14.4).

LOW invariant
-------------
A command may be classified LOW only when the argv form cannot:

- execute arbitrary code or configured external helpers
- write or truncate files
- perform network I/O
- read content outside the workspace

Capability-bearing LOW tools (searchers, ``tree``, ``ps``, ``ls``, and other
path-bearing allowlisted tools) must prove that invariant via explicit checks.
Unknown long options on searchers / ``tree`` / ``ps`` / ``ls`` escalate to
MEDIUM — LOW is earned, not assumed from the executable basename alone.
Readers and read-only git verbs prove the path/helper parts of the invariant
without a full long-option allowlist.

Accepted residual: classification still keys off the basename (PATH substitution
of a LOW name remains LOW by design of the argv executor); path-qualified
executables already escalate out of LOW.
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
# Inert tools take no filesystem/process payload that can violate the LOW
# invariant under shell=False argv execution.
# Truly argv-inert under shell=False: no filesystem payload options we honor.
INERT_LOW_EXECUTABLES: Final = frozenset(
    {"pwd", "true", "false", "uname", "whoami", "which", "echo", "df"}
)
READER_EXECUTABLES: Final = frozenset(
    {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "file", "stat", "du"}
)
SEARCHER_EXECUTABLES: Final = frozenset({"grep", "egrep", "fgrep", "rg"})
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
# Diff/show helpers that can execute configured external programs.
GIT_EXTERNAL_HELPER_OPTIONS: Final = frozenset({"--ext-diff", "--textconv"})
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
# Open-with-default-handler launchers (macOS `open`, Linux `xdg-open`). Not
# read-only: they launch an arbitrary application or navigate to a URL, so they
# stay approval-gated (MEDIUM) rather than auto-running — but they are recognized
# commands, not "unknown executables".
LAUNCHER_COMMANDS: Final = frozenset({"open", "xdg-open"})
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

# Long options that are safe for LOW auto-run on searchers. Anything else
# unknown escalates — LOW must be proven, not assumed from the basename.
SEARCHER_SAFE_LONG_OPTIONS: Final = frozenset(
    {
        "--help",
        "--version",
        "--color",
        "--colour",
        "--include",
        "--exclude",
        "--exclude-dir",
        "--exclude-from",
        "--file",
        "--regexp",
        "--ignore-case",
        "--invert-match",
        "--word-regexp",
        "--line-regexp",
        "--fixed-strings",
        "--basic-regexp",
        "--extended-regexp",
        "--perl-regexp",
        "--count",
        "--files-with-matches",
        "--files-without-match",
        "--only-matching",
        "--no-filename",
        "--with-filename",
        "--line-number",
        "--no-messages",
        "--quiet",
        "--silent",
        "--max-count",
        "--byte-offset",
        "--binary-files",
        "--text",
        "--directories",
        "--devices",
        "--recursive",
        "--dereference-recursive",
        "--no-ignore-case",
        "--heading",
        "--break",
        "--context",
        "--after-context",
        "--before-context",
        "--column",
        "--vimgrep",
        "--json",
        "--debug",
        "--trace",
        "--hidden",
        "--no-hidden",
        "--no-ignore",
        "--no-ignore-vcs",
        "--no-ignore-parent",
        "--no-ignore-global",
        "--ignore-file",
        "--ignore-file-case",
        "--glob",
        "--iglob",
        "--type",
        "--type-not",
        "--type-add",
        "--type-clear",
        "--type-list",
        "--files",
        "--sort",
        "--sortr",
        "--max-depth",
        "--max-filesize",
        "--max-columns",
        "--max-columns-preview",
        "--line-buffered",
        "--block-buffered",
        "--mmap",
        "--no-mmap",
        "--search-zip",
        "--follow",
        "--one-file-system",
        "--no-unicode",
        "--engine",
        "--regexp-size-limit",
        "--dfa-size-limit",
        "--stop-on-nonmatch",
        "--passthru",
        "--null",
        "--null-data",
        "--field-match-separator",
        "--field-context-separator",
        "--path-separator",
        "--hyperlink-format",
        "--stats",
        "--crlf",
        "--no-crlf",
        # Common agent-facing ripgrep options (short forms already stay LOW).
        "--smart-case",
        "--case-sensitive",
        "--multiline",
        "--multiline-dotall",
        "--pcre2",
        "--encoding",
        "--threads",
        "--pretty",
        "--no-config",
    }
)
SEARCHER_EXECUTION_OPTIONS: Final = frozenset({"--pre", "--pre-glob"})
TREE_SAFE_LONG_OPTIONS: Final = frozenset(
    {
        "--help",
        "--version",
        "--noreport",
        "--charset",
        "--filelimit",
        "--si",
        "--du",
        "--inodes",
        "--device",
        "--dirsfirst",
        "--matchdirs",
        "--prune",
        "--ignore",
        "--gitignore",
        "--gitfile",
        "--match",
        "--fromfile",
        "--fflinks",
        "--nolinks",
        "--timefmt",
    }
)
TREE_OUTPUT_OPTIONS: Final = frozenset({"-o", "--output"})
LS_SAFE_LONG_OPTIONS: Final = frozenset(
    {
        "--help",
        "--version",
        "--color",
        "--colour",
        "--group-directories-first",
        "--time-style",
        "--format",
        "--indicator-style",
        "--quoting-style",
        "--block-size",
        "--hide",
        "--ignore",
        "--ignore-backups",
        "--classify",
        "--file-type",
        "--hyperlink",
        "--si",
        "--human-readable",
        "--inode",
        "--size",
        "--recursive",
        "--reverse",
        "--almost-all",
        "--all",
        "--author",
        "--context",
        "--directory",
        "--dired",
        "--full-time",
        "--literal",
        "--numeric-uid-gid",
        "--no-group",
        "--tabsize",
        "--width",
        "--sort",
        "--time",
    }
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


def _long_option_name(token: str) -> str | None:
    if not token.startswith("--") or token == "--":
        return None
    return token.partition("=")[0]


def _unknown_long_option(argv: list[str], allowed: frozenset[str]) -> str | None:
    for token in argv[1:]:
        name = _long_option_name(token)
        if name is not None and name not in allowed:
            return f"unrecognized option {name}"
    return None


def _option_present(argv: list[str], names: frozenset[str]) -> str | None:
    """Return the matching option name when present (``--opt`` or ``--opt=``)."""
    for token in argv[1:]:
        if token in names:
            return token
        name = _long_option_name(token)
        if name is not None and name in names:
            return name
    return None


def _short_option_letters(names: frozenset[str]) -> frozenset[str]:
    return frozenset(
        name[1:]
        for name in names
        if name.startswith("-") and not name.startswith("--") and len(name) == 2
    )


def _split_option_value(argv: list[str], names: frozenset[str]) -> str | None:
    """Value of a space-separated, ``=``-attached, glued, or clustered option.

    Clustered short options are supported when the value-taking letter is last
    in the cluster (``-ao out.txt``) or followed by a glued value (``-aofoo``).
    """
    short_letters = _short_option_letters(names)
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in names:
            return argv[index + 1] if index + 1 < len(argv) else None
        name = _long_option_name(token)
        if name is not None and name in names and "=" in token:
            return token.split("=", 1)[1]
        if token.startswith("-") and not token.startswith("--"):
            body = token[1:]
            # Exact/glued short form: -o / -oFILE
            for name in names:
                if name.startswith("-") and not name.startswith("--") and token.startswith(name):
                    glued = token[len(name) :]
                    if glued:
                        return glued
            # Clustered short options: -ao FILE or -aofoo
            for offset, letter in enumerate(body):
                if letter not in short_letters:
                    continue
                rest = body[offset + 1 :]
                if rest:
                    return rest
                return argv[index + 1] if index + 1 < len(argv) else None
        index += 1
    return None


def _short_flag_letter_present(argv: list[str], letter: str) -> bool:
    """True when ``letter`` appears in any short-option cluster (e.g. ``-aR``)."""
    for token in argv[1:]:
        if token.startswith("-") and not token.startswith("--") and letter in token[1:]:
            return True
    return False


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


def _git_external_helper(flags: list[str]) -> str | None:
    for flag in flags:
        name = flag.partition("=")[0]
        if name in GIT_EXTERNAL_HELPER_OPTIONS:
            return f"{name} can execute configured external helpers"
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
    helper = _git_external_helper(flags)
    if helper:
        return CommandRisk(RiskLevel.MEDIUM, (helper,))
    if verb in GIT_READONLY_VERBS or (
        verb == "stash" and verb_args and verb_args[0] in ("list", "show")
    ):
        # Read-only git still has to prove the LOW invariant: no out-of-workspace
        # path payloads (e.g. `git diff --no-index /etc/a /etc/b`).
        outside = _path_arg_outside_workspace(argv, workspace)
        if outside:
            return CommandRisk(
                RiskLevel.HIGH,
                (f"git {verb or '?'} reads outside the workspace boundary: {outside}",),
            )
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


def _classify_searcher(argv: list[str], workspace: Path) -> CommandRisk | None:
    """Extra LOW-invariant checks for grep/rg family. None = fall through."""
    hook = _option_present(argv, SEARCHER_EXECUTION_OPTIONS)
    if hook:
        return CommandRisk(
            RiskLevel.HIGH,
            (f"{hook} executes a preprocessor over matched files",),
        )
    unknown = _unknown_long_option(argv, SEARCHER_SAFE_LONG_OPTIONS)
    if unknown:
        return CommandRisk(RiskLevel.MEDIUM, (unknown,))
    outside = _path_arg_outside_workspace(argv, workspace)
    if outside:
        return CommandRisk(RiskLevel.HIGH, (f"reads outside the workspace boundary: {outside}",))
    return None


def _classify_tree(argv: list[str], workspace: Path) -> CommandRisk | None:
    # Debian/GNU tree -R writes 00Tree.html at each level (ala -o).
    if _short_flag_letter_present(argv, "R"):
        return CommandRisk(RiskLevel.MEDIUM, ("tree -R writes HTML files into the tree",))
    output = _split_option_value(argv, TREE_OUTPUT_OPTIONS)
    if (
        output is not None
        or _option_present(argv, TREE_OUTPUT_OPTIONS)
        or _short_flag_letter_present(argv, "o")
    ):
        target = output or ""
        if target and _path_arg_outside_workspace(["tree", target], workspace):
            return CommandRisk(
                RiskLevel.HIGH,
                ("tree output path is outside the workspace boundary",),
            )
        return CommandRisk(RiskLevel.MEDIUM, ("tree writes output to a file",))
    unknown = _unknown_long_option(argv, TREE_SAFE_LONG_OPTIONS)
    if unknown:
        return CommandRisk(RiskLevel.MEDIUM, (unknown,))
    outside = _path_arg_outside_workspace(argv, workspace)
    if outside:
        return CommandRisk(RiskLevel.HIGH, (f"reads outside the workspace boundary: {outside}",))
    return None


def _format_field_list(value: str) -> list[str]:
    return [part.strip().lower() for part in value.replace(" ", ",").split(",") if part.strip()]


def _ps_format_exposes_environment(argv: list[str]) -> bool:
    """True when ``-o``/``-O``/``--format`` selects env/environ columns."""
    format_names = frozenset({"-o", "-O", "--format", "--Format"})
    index = 1
    while index < len(argv):
        token = argv[index]
        value: str | None = None
        if token in format_names:
            value = argv[index + 1] if index + 1 < len(argv) else None
            index += 2
        elif token.startswith("--format=") or token.startswith("--Format="):
            value = token.split("=", 1)[1]
            index += 1
        elif token.startswith("-o") and len(token) > 2:
            value = token[2:]
            index += 1
        elif token.startswith("-O") and len(token) > 2:
            value = token[2:]
            index += 1
        else:
            # Clustered short options with trailing o/O: -ao environ / -aopid,environ
            if token.startswith("-") and not token.startswith("--"):
                body = token[1:]
                for offset, letter in enumerate(body):
                    if letter not in {"o", "O"}:
                        continue
                    rest = body[offset + 1 :]
                    value = rest if rest else (argv[index + 1] if index + 1 < len(argv) else None)
                    break
            index += 1
        if value is None:
            continue
        fields = _format_field_list(value)
        if any(field in {"env", "environ", "environment"} for field in fields):
            return True
    return False


def _ps_exposes_environment(argv: list[str]) -> bool:
    """True when argv likely requests process environment display.

    BSD ``ps e`` / ``ps auxe`` expose environments. macOS ``-E`` does too.
    SysV ``-e`` means "every process" and is left alone (listing only).
    Format selectors (``-o environ``, ``--format=pid,env``) are also covered.
    """
    if _ps_format_exposes_environment(argv):
        return True
    for token in argv[1:]:
        if token in {"e", "E"}:
            return True
        name = _long_option_name(token)
        if name is not None and "env" in name.lower():
            return True
        if not token.startswith("-") and token.isalpha() and "e" in token.lower():
            # BSD clustered flags without a leading dash: auxe, ue, ...
            return True
        if token.startswith("-") and not token.startswith("--") and "E" in token[1:]:
            return True
    return False


def _classify_ps(argv: list[str]) -> CommandRisk | None:
    if _ps_exposes_environment(argv):
        return CommandRisk(RiskLevel.MEDIUM, ("ps can expose process environments",))
    # Unknown long options fail the LOW proof.
    unknown = _unknown_long_option(
        argv,
        frozenset(
            {
                "--help",
                "--version",
                "--pid",
                "--ppid",
                "--user",
                "--sort",
                "--format",
                "--forest",
                "--cols",
                "--columns",
                "--width",
                "--headers",
                "--no-headers",
                "--deselect",
            }
        ),
    )
    if unknown:
        return CommandRisk(RiskLevel.MEDIUM, (unknown,))
    return None


def _classify_ls(argv: list[str], workspace: Path) -> CommandRisk | None:
    unknown = _unknown_long_option(argv, LS_SAFE_LONG_OPTIONS)
    if unknown:
        return CommandRisk(RiskLevel.MEDIUM, (unknown,))
    outside = _path_arg_outside_workspace(argv, workspace)
    if outside:
        return CommandRisk(RiskLevel.HIGH, (f"reads outside the workspace boundary: {outside}",))
    return None


def _date_file_operands(argv: list[str]) -> list[str]:
    """Paths that ``date`` may read via ``-r``/``-f``/``--file``/``--reference``."""
    names = frozenset({"-r", "-f", "--file", "--reference"})
    short_letters = frozenset({"r", "f"})
    found: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in names:
            if index + 1 < len(argv):
                found.append(argv[index + 1])
            index += 2
            continue
        name = _long_option_name(token)
        if name in {"--file", "--reference"} and "=" in token:
            found.append(token.split("=", 1)[1])
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--"):
            body = token[1:]
            # Exact/glued: -rPATH / -fPATH
            glued = False
            for letter in short_letters:
                prefix = f"-{letter}"
                if token.startswith(prefix) and len(token) > 2:
                    found.append(token[2:])
                    glued = True
                    break
            if not glued:
                # Clustered: -ur PATH (value-taking letter last)
                for offset, letter in enumerate(body):
                    if letter not in short_letters:
                        continue
                    rest = body[offset + 1 :]
                    if rest:
                        found.append(rest)
                    elif index + 1 < len(argv):
                        found.append(argv[index + 1])
                    break
        index += 1
    return found


def _classify_date(argv: list[str], workspace: Path) -> CommandRisk | None:
    operands = _date_file_operands(argv)
    if operands:
        outside = _path_arg_outside_workspace(["date", *operands], workspace)
        if outside:
            return CommandRisk(
                RiskLevel.HIGH,
                (f"reads outside the workspace boundary: {outside}",),
            )
        # In-workspace file-backed date reads are still a filesystem payload —
        # ask rather than auto-run under the LOW invariant.
        return CommandRisk(RiskLevel.MEDIUM, ("date reads timestamps from a file",))
    outside = _path_arg_outside_workspace(argv, workspace)
    if outside:
        return CommandRisk(RiskLevel.HIGH, (f"reads outside the workspace boundary: {outside}",))
    return None


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
    if executable in LAUNCHER_COMMANDS:
        return CommandRisk(RiskLevel.MEDIUM, (f"{executable} launches an application or URL",))
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

    # Capability-bearing LOW candidates: prove the invariant before AUTO.
    if executable in SEARCHER_EXECUTABLES:
        special = _classify_searcher(argv, workspace)
        if special is not None:
            return special
        return CommandRisk(RiskLevel.LOW, ())
    if executable == "tree":
        special = _classify_tree(argv, workspace)
        if special is not None:
            return special
        return CommandRisk(RiskLevel.LOW, ())
    if executable == "ps":
        special = _classify_ps(argv)
        if special is not None:
            return special
        return CommandRisk(RiskLevel.LOW, ())
    if executable == "ls":
        special = _classify_ls(argv, workspace)
        if special is not None:
            return special
        return CommandRisk(RiskLevel.LOW, ())
    if executable == "date":
        special = _classify_date(argv, workspace)
        if special is not None:
            return special
        return CommandRisk(RiskLevel.LOW, ())
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
        return CommandRisk(RiskLevel.LOW, ())
    if executable in INERT_LOW_EXECUTABLES or executable in LOW_EXECUTABLES:
        # Remaining LOW allowlist entries. Non-inert leftovers (e.g. date) still
        # get a boundary check so out-of-workspace reads cannot auto-run.
        if executable not in INERT_LOW_EXECUTABLES:
            outside = _path_arg_outside_workspace(argv, workspace)
            if outside:
                return CommandRisk(
                    RiskLevel.HIGH,
                    (f"reads outside the workspace boundary: {outside}",),
                )
        return CommandRisk(RiskLevel.LOW, ())

    return CommandRisk(
        RiskLevel.MEDIUM, (f"unknown executable {executable!r}; defaulting to medium",)
    )
