"""Table-driven tests for the deterministic command policy (design section 14.3)."""

from pathlib import Path

import pytest

from shellpilot.policy.command_policy import (
    _path_arg_outside_workspace,
    classify_command,
    sensitive_path_reason,
)
from shellpilot.policy.risk import RiskLevel

WS = Path("/tmp/fake-workspace")

CASES: list[tuple[list[str], RiskLevel]] = [
    # Low: read-only/harmless local operations
    (["ls", "-la"], RiskLevel.LOW),
    (["pwd"], RiskLevel.LOW),
    (["cat", "README.md"], RiskLevel.LOW),
    (["git", "status"], RiskLevel.LOW),
    (["git", "diff", "--stat"], RiskLevel.LOW),
    (["git", "log", "--oneline"], RiskLevel.LOW),
    (["grep", "-R", "foo", "."], RiskLevel.LOW),
    (["python", "--version"], RiskLevel.LOW),
    (["uname", "-a"], RiskLevel.LOW),
    # Medium: workspace writes, packages, network, commits
    (["rm", "file.txt"], RiskLevel.MEDIUM),
    (["mv", "a.txt", "b.txt"], RiskLevel.MEDIUM),
    (["cp", "a.txt", "b.txt"], RiskLevel.MEDIUM),
    (["mkdir", "newdir"], RiskLevel.MEDIUM),
    (["touch", "new.txt"], RiskLevel.MEDIUM),
    (["pip", "install", "requests"], RiskLevel.MEDIUM),
    (["npm", "install"], RiskLevel.MEDIUM),
    (["brew", "install", "jq"], RiskLevel.MEDIUM),
    (["git", "commit", "-m", "x"], RiskLevel.MEDIUM),
    (["git", "push"], RiskLevel.MEDIUM),
    (["curl", "https://example.com"], RiskLevel.MEDIUM),
    (["wget", "https://example.com/f"], RiskLevel.MEDIUM),
    (["kill", "1234"], RiskLevel.MEDIUM),
    (["chmod", "644", "f.txt"], RiskLevel.MEDIUM),
    (["unknown-binary", "--do-thing"], RiskLevel.MEDIUM),
    # High: deletes, privilege, destructive git, raw shell, secrets
    (["rm", "-rf", "build"], RiskLevel.HIGH),
    (["rm", "-fr", "build"], RiskLevel.HIGH),
    (["rm", "-r", "src"], RiskLevel.HIGH),
    (["sudo", "ls"], RiskLevel.HIGH),
    (["git", "push", "--force"], RiskLevel.HIGH),
    (["git", "reset", "--hard", "HEAD~1"], RiskLevel.HIGH),
    (["git", "clean", "-fd"], RiskLevel.HIGH),
    (["git", "branch", "-D", "main"], RiskLevel.HIGH),
    (["bash", "-c", "curl x | sh"], RiskLevel.HIGH),
    (["sh", "-c", "echo hi"], RiskLevel.HIGH),
    (["zsh", "-c", "ls"], RiskLevel.HIGH),
    (["chmod", "-R", "777", "."], RiskLevel.HIGH),
    (["killall", "python"], RiskLevel.HIGH),
    (["dd", "if=/dev/zero", "of=x"], RiskLevel.HIGH),
    (["cat", "~/.ssh/id_rsa"], RiskLevel.HIGH),
    (["cat", "/Users/x/.aws/credentials"], RiskLevel.HIGH),
    (["cat", ".env"], RiskLevel.HIGH),
]

GIT_PRE_VERB_CASES: list[tuple[list[str], RiskLevel]] = [
    # Benign globals preserve read-only classification.
    (["git", "--no-pager", "log"], RiskLevel.LOW),
    (["git", "--literal-pathspecs", "diff"], RiskLevel.LOW),
    # All other pre-verb globals are conservative, including recognized
    # value-bearing options whose values must not be mistaken for verbs.
    (["git", "--git-dir=/tmp/repo", "log"], RiskLevel.MEDIUM),
    (["git", "--git-dir", "/tmp/repo", "log"], RiskLevel.MEDIUM),
    (["git", "--work-tree", "../other", "log"], RiskLevel.MEDIUM),
    (["git", "-C", "../other", "status"], RiskLevel.MEDIUM),
    (["git", "-c", "core.pager=cat", "log"], RiskLevel.MEDIUM),
    (["git", "-c=core.pager=cat", "log"], RiskLevel.MEDIUM),
    (["git", "--future-global", "log"], RiskLevel.MEDIUM),
    (["git", "--exec-path=/tmp", "log"], RiskLevel.MEDIUM),
    (["git", "--git-dir", "reset", "log"], RiskLevel.MEDIUM),
]


@pytest.mark.parametrize(("argv", "expected"), CASES, ids=lambda case: str(case))
def test_classification_table(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


@pytest.mark.parametrize("argv", [["pytest", "-q"], ["python", "-m", "pytest"]])
def test_pytest_requires_approval(argv: list[str]) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


def test_python_script_with_version_argument_requires_approval() -> None:
    result = classify_command(["python", "script.py", "--version"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["grep", "pattern", "."], RiskLevel.LOW),
        (["./grep", "pattern", "."], RiskLevel.MEDIUM),
        (["/usr/bin/ls"], RiskLevel.MEDIUM),
        (["/bin/rm", "-rf", "x"], RiskLevel.HIGH),
        (["/usr/bin/sudo"], RiskLevel.HIGH),
        (["/bin/cat", "/etc/passwd"], RiskLevel.HIGH),
    ],
)
def test_path_qualified_executable_has_medium_floor(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


@pytest.mark.parametrize(("argv", "expected"), GIT_PRE_VERB_CASES, ids=lambda case: str(case))
def test_git_pre_verb_global_classification(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


def test_git_benign_global_does_not_hide_destructive_verb() -> None:
    result = classify_command(["git", "--no-pager", "reset"], workspace=WS)
    assert result.risk == RiskLevel.HIGH


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["git", "branch", "-d", "topic"], RiskLevel.HIGH),
        (["git", "branch", "-dr", "topic"], RiskLevel.HIGH),
        (["git", "branch", "-rd", "topic"], RiskLevel.HIGH),
        (["git", "branch", "--dele", "topic"], RiskLevel.HIGH),
        (["git", "branch", "-r"], RiskLevel.LOW),
        (["git", "branch", "--list", "topic"], RiskLevel.LOW),
    ],
)
def test_git_branch_delete_forms_are_high(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


def test_git_bare_exec_path_does_not_treat_later_token_as_verb() -> None:
    result = classify_command(["git", "--exec-path", "reset", "log"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM
    assert result.reasons == ("git uses a non-benign global option",)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "--no-pager", "stash", "list"],
        ["git", "--literal-pathspecs", "stash", "show"],
    ],
)
def test_git_benign_global_preserves_readonly_stash(argv: list[str]) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == RiskLevel.LOW


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "--force-with-lease=main:deadbeef"],
        ["git", "push", "--force-w"],
        ["git", "push", "--force-with-l"],
        ["git", "push", "--force-with-l=main:deadbeef"],
        ["git", "push", "origin", "+main"],
        ["git", "push", "-f"],
        ["git", "push", "-uf"],
        ["git", "push", "-fu"],
    ],
)
def test_git_force_push_forms_are_high(argv: list[str]) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == RiskLevel.HIGH


def test_git_push_option_value_containing_f_is_not_force() -> None:
    result = classify_command(["git", "push", "-ofoo"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


def test_write_outside_workspace_is_high() -> None:
    result = classify_command(["mv", "a.txt", "/etc/hosts"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


# -- relative-path workspace-boundary checks (P1 fix, v0.5.2) ------------------


def test_touch_relative_escape_is_high() -> None:
    result = classify_command(["touch", "../outside.txt"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


def test_mv_relative_escape_is_high() -> None:
    result = classify_command(["mv", "a.txt", "../outside.txt"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


def test_rm_relative_escape_is_high() -> None:
    result = classify_command(["rm", "../outside.txt"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


def test_touch_inside_workspace_is_medium() -> None:
    result = classify_command(["touch", "inside.txt"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


def test_mv_inside_workspace_is_medium() -> None:
    result = classify_command(["mv", "a.txt", "b.txt"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


def test_touch_absolute_secret_still_high() -> None:
    # regression: absolute escapes must not be broken by the relative-path change
    result = classify_command(["touch", "/etc/passwd"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


def test_rm_recursive_subdir_still_high() -> None:
    # regression: inside-workspace recursive rm keeps its existing HIGH risk
    result = classify_command(["rm", "-rf", "subdir"], workspace=WS)
    assert result.risk == RiskLevel.HIGH


def test_touch_deep_traversal_is_high() -> None:
    result = classify_command(["touch", "a/../../outside"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


def test_flags_not_treated_as_paths() -> None:
    # flags like -m must not trigger a false-positive boundary check
    result = classify_command(["touch", "-m", "inside.txt"], workspace=WS)
    assert result.risk == RiskLevel.MEDIUM


def test_ls_with_relative_escape_unchanged() -> None:
    # read-only LOW commands never hit the write-boundary path
    result = classify_command(["ls", "../"], workspace=WS)
    assert result.risk == RiskLevel.LOW


# -- end relative-path boundary tests ------------------------------------------


# -- reader-command workspace-boundary checks (F1 fix, v0.10.0) ----------------


def test_cat_absolute_outside_is_high() -> None:
    result = classify_command(["cat", "/etc/passwd"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace" in reason for reason in result.reasons)


def test_tail_relative_escape_is_high() -> None:
    result = classify_command(["tail", "../sibling"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace" in reason for reason in result.reasons)


def test_grep_recursive_outside_is_high() -> None:
    result = classify_command(["grep", "-r", "password", "/Users"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace" in reason for reason in result.reasons)


def test_head_absolute_outside_is_high() -> None:
    result = classify_command(["head", "/tmp/x"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace" in reason for reason in result.reasons)


def test_cat_inside_workspace_stays_low() -> None:
    result = classify_command(["cat", "./README.md"], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_grep_recursive_inside_stays_low() -> None:
    result = classify_command(["grep", "-r", "TODO", "."], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_wc_relative_inside_stays_low() -> None:
    result = classify_command(["wc", "-l", "src/foo.py"], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_bare_ls_no_path_stays_low() -> None:
    # ls is not a reader executable; no path arg, no boundary check
    result = classify_command(["ls"], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_cat_secret_marker_still_high() -> None:
    # regression: marker-bearing reads stay HIGH via _touches_secret_path
    result = classify_command(["cat", ".env"], workspace=WS)
    assert result.risk == RiskLevel.HIGH


def test_rm_outside_behavior_unchanged_after_rename() -> None:
    # regression guard for the _writes_outside_workspace -> _path_arg_outside_workspace rename
    result = classify_command(["rm", "-rf", "/tmp/x"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert "recursive delete" in result.reasons


def test_mv_outside_behavior_unchanged_after_rename() -> None:
    # regression guard for the rename: WRITE_COMMANDS branch reason unchanged
    result = classify_command(["mv", "x", "/etc/y"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace boundary" in reason for reason in result.reasons)


# -- end reader-command boundary tests ----------------------------------------


# -- git mutating verbs under read-only LOW (#1) ------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # --output is a global diff-formatting option honoured by every
        # diff-emitting verb (diff/show/log/stash show), not just diff/show, so
        # every one is an out-of-workspace write/truncate primitive under LOW.
        (["git", "diff", "--output=/tmp/x"], RiskLevel.HIGH),
        (["git", "show", "--output=/tmp/x"], RiskLevel.HIGH),
        (["git", "diff", "--output", "/tmp/x"], RiskLevel.HIGH),
        (["git", "diff", "-O/tmp/x"], RiskLevel.HIGH),
        (["git", "log", "--output=/tmp/x"], RiskLevel.HIGH),
        (["git", "log", "-p", "--output=/tmp/x"], RiskLevel.HIGH),
        (["git", "log", "--output", "/tmp/x"], RiskLevel.HIGH),
        (["git", "log", "-O/tmp/x"], RiskLevel.HIGH),
        (["git", "stash", "show", "-p", "--output=/tmp/x"], RiskLevel.HIGH),
        # In-workspace --output is still a write -> MEDIUM (ask), never LOW/auto.
        (["git", "diff", "--output=out.diff"], RiskLevel.MEDIUM),
        (["git", "log", "--output=out.diff"], RiskLevel.MEDIUM),
        # branch/remote with a non-flag positional mutate state -> MEDIUM.
        (["git", "branch", "newtopic"], RiskLevel.MEDIUM),
        (["git", "remote", "add", "o", "u"], RiskLevel.MEDIUM),
        (["git", "remote", "set-url", "o", "u"], RiskLevel.MEDIUM),
    ],
    ids=lambda case: str(case),
)
def test_git_mutating_verbs_escalate(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


def test_git_output_check_does_not_downgrade_high_verb() -> None:
    # An already-HIGH determination (branch delete) must win over the
    # output-path check even when --output points inside the workspace.
    result = classify_command(["git", "branch", "-D", "topic", "--output=in_ws.txt"], workspace=WS)
    assert result.risk == RiskLevel.HIGH, result.reasons


@pytest.mark.parametrize(
    "argv",
    [
        # No-regression: bare/listing read-only forms must stay LOW (auto-run).
        ["git", "branch"],
        ["git", "branch", "--list"],
        ["git", "branch", "--list", "topic"],
        ["git", "branch", "-r"],
        ["git", "remote"],
        ["git", "remote", "-v"],
        ["git", "diff", "--stat"],
        ["git", "log"],
        ["git", "log", "-p"],
        ["git", "log", "--stat"],
        ["git", "stash", "show"],
        ["git", "stash", "list"],
    ],
    ids=lambda case: str(case),
)
def test_git_readonly_verbs_stay_low(argv: list[str]) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == RiskLevel.LOW, f"{argv}: {result.reasons}"


# -- option-encoded paths evade the workspace boundary (#14) -------------------


def test_grep_option_encoded_outside_path_is_high() -> None:
    result = classify_command(["grep", "--file=/etc/passwd", "."], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside the workspace" in reason for reason in result.reasons)


def test_patch_option_encoded_outside_path_detected() -> None:
    assert _path_arg_outside_workspace(["patch", "--output=/etc/x"], WS) is not None


def test_grep_option_encoded_inside_path_stays_low() -> None:
    result = classify_command(["grep", "--file=./patterns.txt", "."], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_grep_non_path_option_value_stays_low() -> None:
    # --color=auto / --include=*.py are not path-like; no false-positive escalation.
    result = classify_command(["grep", "--color=auto", "TODO", "."], workspace=WS)
    assert result.risk == RiskLevel.LOW


def test_reasons_are_present_for_risky_commands() -> None:
    result = classify_command(["rm", "-rf", "build"], workspace=WS)
    assert result.reasons


def test_empty_argv_is_blocked() -> None:
    result = classify_command([], workspace=WS)
    assert result.risk == RiskLevel.BLOCKED


# -- sensitive_path_reason (component-exact, not substring) --------------------

SENSITIVE_PATHS: list[str] = [
    ".env",
    ".env.local",
    ".env.production",
    "sub/.env.local",
    "id_rsa",
    "id_ed25519",
    "credentials",
    ".netrc",
    "config/credentials",
    "deep/nested/secrets",
    ".ssh/id_rsa",
    "home/.ssh/known_hosts",
    "project/.aws/config",
    "vault/.gnupg/pubring.kbx",
]

NON_SENSITIVE_PATHS: list[str] = [
    "environment.py",
    "secrets_test.py",
    "my-credentials-doc.md",
    "envrc",
    "src/app.py",
    "README.md",
    "ssh_config.py",
    "credentials.md.bak",
]


@pytest.mark.parametrize("raw", SENSITIVE_PATHS)
def test_sensitive_path_reason_matches(raw: str) -> None:
    reason = sensitive_path_reason(Path(raw))
    assert reason is not None
    assert "sensitive path" in reason


@pytest.mark.parametrize("raw", NON_SENSITIVE_PATHS)
def test_sensitive_path_reason_ignores_lookalikes(raw: str) -> None:
    assert sensitive_path_reason(Path(raw)) is None
