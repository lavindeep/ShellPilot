"""Tests for deterministic risk-explanation templates.

The drift-guard coverage test below is the load-bearing one: it walks a
representative argv for every reason-producing branch of the deterministic
policy — both ``classify_command`` AND ``sensitive_path_reason`` — and asserts
each ``reasons`` tuple resolves to the EXACT expected sentence (not merely a
non-FALLBACK string). If someone adds or renames a policy reason without
updating ``shellpilot/policy/explanations.py``, or renames a reason so it
drifts onto the wrong needle, that test fails loudly.
"""

from pathlib import Path

import pytest

from shellpilot.policy.command_policy import (
    CommandRisk,
    classify_command,
    sensitive_path_reason,
)
from shellpilot.policy.explanations import FALLBACK, explain_risk
from shellpilot.policy.risk import RiskLevel

WS = Path("/tmp/fake-workspace")


# -- per-reason mapping --------------------------------------------------------

PER_REASON_CASES: list[tuple[str, str]] = [
    (
        "sudo is privileged or system-destructive",
        "Runs with elevated privileges and can make system-wide changes that may be irreversible.",
    ),
    (
        "bash runs raw shell syntax; use Manual Shell instead",
        "Launches a raw shell that can execute arbitrary, unchecked commands.",
    ),
    (
        "recursive delete",
        "Recursively and permanently deletes the target and everything inside it; "
        "this cannot be undone.",
    ),
    (
        "glob delete",
        "Deletes every file matching the pattern; the set is expanded at run time "
        "and cannot be undone.",
    ),
    (
        "deletes outside the workspace",
        "Deletes files outside the workspace directory.",
    ),
    (
        "git reset can destroy local changes",
        "Can discard or overwrite uncommitted local changes, which cannot be recovered.",
    ),
    (
        "git branch deletion",
        "Deletes a git branch.",
    ),
    (
        "force push rewrites remote history",
        "Force-pushes, rewriting published remote history and possibly overwriting "
        "others' commits.",
    ),
    (
        "recursive permission change",
        "Recursively changes permissions or ownership across a directory tree.",
    ),
    (
        "find with -delete/-exec can modify files",
        "Runs find with -delete/-exec, which can modify or remove matched files.",
    ),
    (
        "argument '.env' looks like a credential/secret path",
        "Touches a path that looks like a credential or secret file.",
    ),
    (
        "target ../outside is outside the workspace boundary",
        "Writes to a path outside the workspace directory.",
    ),
]


@pytest.mark.parametrize(("reason", "expected"), PER_REASON_CASES)
def test_per_reason_mapping(reason: str, expected: str) -> None:
    assert explain_risk((reason,)) == expected


def test_per_reason_from_real_classification() -> None:
    # Build the reason via the real classifier rather than hardcoding it.
    result = classify_command(["rm", "-rf", "build"], workspace=WS)
    assert result.reasons == ("recursive delete",)
    assert explain_risk(result.reasons) == (
        "Recursively and permanently deletes the target and everything inside it; "
        "this cannot be undone."
    )


# -- multi-reason composition --------------------------------------------------


def test_multi_reason_composition() -> None:
    reasons = (
        "force push rewrites remote history",
        "argument '.env' looks like a credential/secret path",
    )
    out = explain_risk(reasons)
    force_sentence = (
        "Force-pushes, rewriting published remote history and possibly overwriting others' commits."
    )
    cred_sentence = "Touches a path that looks like a credential or secret file."
    assert force_sentence in out
    assert cred_sentence in out
    # space-joined, single space between the two sentences
    assert out == f"{force_sentence} {cred_sentence}"


def test_multi_reason_dedupes_preserving_order() -> None:
    reasons = (
        "recursive delete",
        "argument 'x' looks like a credential/secret path",
        "recursive delete",
    )
    out = explain_risk(reasons)
    recursive = (
        "Recursively and permanently deletes the target and everything inside it; "
        "this cannot be undone."
    )
    cred = "Touches a path that looks like a credential or secret file."
    assert out == f"{recursive} {cred}"
    assert out.count(recursive) == 1


# -- specific-before-general ordering ------------------------------------------


def test_recursive_permission_change_beats_permission_change() -> None:
    # "recursive permission change" contains "permission change" as a substring;
    # the specific sentence must win.
    out = explain_risk(("recursive permission change",))
    assert out == "Recursively changes permissions or ownership across a directory tree."


# -- empty / unknown fall through ----------------------------------------------


def test_empty_reasons_returns_fallback() -> None:
    assert explain_risk(()) == FALLBACK


def test_unknown_reason_returns_fallback() -> None:
    assert explain_risk(("some reason that matches nothing",)) == FALLBACK


# -- drift-guard coverage (critical) -------------------------------------------

# One representative argv per reason-producing branch of classify_command,
# each paired with the EXACT sentence its reason must resolve to (derived from
# the needle->sentence map in explanations.py). Pinning the expected sentence
# catches wrong-needle drift: a renamed reason that still substring-matched some
# OTHER needle would map to the wrong sentence yet stay non-FALLBACK.
# Mirrors the case style in tests/test_command_policy.py.
DRIFT_CASES: list[tuple[list[str], str]] = [
    # HIGH families
    (
        ["sudo", "ls"],  # privileged/system-destructive
        "Runs with elevated privileges and can make system-wide changes that may be irreversible.",
    ),
    (
        ["bash", "-c", "echo hi"],  # raw shell syntax
        "Launches a raw shell that can execute arbitrary, unchecked commands.",
    ),
    (
        ["rm", "-rf", "build"],  # recursive delete
        "Recursively and permanently deletes the target and everything inside it; "
        "this cannot be undone.",
    ),
    (
        ["rm", "build/*"],  # glob delete
        "Deletes every file matching the pattern; the set is expanded at run time "
        "and cannot be undone.",
    ),
    (
        ["rm", "../outside"],  # deletes outside the workspace
        "Deletes files outside the workspace directory.",
    ),
    (
        ["git", "reset", "--hard", "HEAD~1"],  # git reset can destroy local changes
        "Can discard or overwrite uncommitted local changes, which cannot be recovered.",
    ),
    (
        ["git", "clean", "-fd"],  # git clean can destroy local changes
        "Can discard or overwrite uncommitted local changes, which cannot be recovered.",
    ),
    (
        ["git", "branch", "-D", "main"],  # git branch deletion
        "Deletes a git branch.",
    ),
    (
        ["git", "push", "--force"],  # force push
        "Force-pushes, rewriting published remote history and possibly overwriting "
        "others' commits.",
    ),
    (
        ["chmod", "-R", "777", "."],  # recursive permission change
        "Recursively changes permissions or ownership across a directory tree.",
    ),
    (
        ["find", ".", "-delete"],  # find -delete can modify files
        "Runs find with -delete/-exec, which can modify or remove matched files.",
    ),
    (
        ["cat", ".env"],  # credential/secret path
        "Touches a path that looks like a credential or secret file.",
    ),
    (
        ["mv", "a.txt", "/etc/hosts"],  # outside the workspace boundary
        "Writes to a path outside the workspace directory.",
    ),
    # MEDIUM families
    (
        ["rm", "file.txt"],  # deletes a file
        "Deletes a file in the workspace.",
    ),
    (
        ["git", "push"],  # git push publishes commits
        "Publishes local commits to the remote.",
    ),
    (
        ["git", "commit", "-m", "x"],  # changes repository state
        "Changes git repository state.",
    ),
    (
        ["chmod", "644", "f.txt"],  # permission change
        "Changes file permissions or ownership.",
    ),
    (
        ["pip", "install", "requests"],  # package operation
        "Installs or modifies packages, which can run setup code and change your environment.",
    ),
    (
        ["curl", "https://example.com"],  # network activity
        "Performs network activity (sends or fetches data over the network).",
    ),
    (
        ["mv", "a.txt", "b.txt"],  # writes to the workspace
        "Writes to files in the workspace.",
    ),
    (
        ["kill", "1234"],  # signals a process
        "Sends a signal to another process.",
    ),
    (
        ["python", "script.py"],  # runs arbitrary python code
        "Executes arbitrary Python code.",
    ),
    (
        ["unknown-binary", "--do-thing"],  # defaulting to medium
        "Runs an unrecognized executable; its effects can't be determined in advance.",
    ),
]


@pytest.mark.parametrize(("argv", "expected"), DRIFT_CASES, ids=lambda case: str(case))
def test_drift_guard_every_reason_has_a_template(argv: list[str], expected: str) -> None:
    result: CommandRisk = classify_command(argv, workspace=WS)
    # Sanity: these argvs are all expected to be risky and carry reasons.
    assert result.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert result.reasons, f"{argv} produced no reasons"
    out = explain_risk(result.reasons)
    assert out, f"{argv}: explanation was empty"
    assert out != FALLBACK, (
        f"{argv} -> reasons {result.reasons!r} fell through to FALLBACK; "
        "add a template needle in shellpilot/policy/explanations.py"
    )
    # Full chain: argv -> classify -> reasons -> explain_risk -> exact sentence.
    # Each DRIFT_CASES argv yields a single reason, so the explanation must equal
    # exactly the expected sentence (catches wrong-needle drift).
    assert out == expected, (
        f"{argv} -> reasons {result.reasons!r} mapped to {out!r}, "
        f"expected {expected!r}; a classifier reason likely drifted onto the "
        "wrong needle in shellpilot/policy/explanations.py"
    )


def test_drift_guard_sensitive_path_reason_has_a_template() -> None:
    # The "sensitive path" reason is produced by sensitive_path_reason(), NOT by
    # classify_command, so it is invisible to the argv-driven cases above. Cover
    # it explicitly so the drift guard truly spans every reason-producing branch
    # of the deterministic policy.
    reason = sensitive_path_reason(Path(".env"))
    assert reason == "reads a sensitive path (.env)"
    out = explain_risk((reason,))
    assert out != FALLBACK
    assert out == "Reads a file that looks like a credential or secret."
