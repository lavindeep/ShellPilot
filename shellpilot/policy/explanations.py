"""Deterministic, honest explanations for high-risk command approval.

The command classifier in :mod:`shellpilot.policy.command_policy` already knows
*why* a command is risky (it returns free-form English ``reasons``). This module
turns those reasons into fuller, one-line consequence sentences with a pure
function — no model call, no I/O — so the approval prompt can appear instantly.

The needle map below mirrors the reason strings produced by
``command_policy.classify_command``; some of those reasons embed runtime values
(``{executable}``, ``{verb}``, ``{token}``, ``{component}``), so matching is by
stable substring, never exact equality. The drift-guard coverage test in
``tests/test_explanations.py`` keeps this map in sync with the classifier: it
fails loudly if a classifier reason is added or renamed without a template here.
"""

from __future__ import annotations

from typing import Final

FALLBACK: Final[str] = (
    "This is a high-risk operation with potentially destructive or irreversible effects."
)

# Ordered (needle, sentence) pairs. For each classifier reason, the FIRST pair
# whose needle is a substring of the reason wins. Specific needles MUST precede
# the more general needles they overlap with (e.g. "recursive permission change"
# before "permission change").
_TEMPLATES: Final[tuple[tuple[str, str], ...]] = (
    # -- HIGH families --------------------------------------------------------
    (
        "is privileged or system-destructive",
        "Runs with elevated privileges and can make system-wide changes that may be irreversible.",
    ),
    (
        "runs raw shell syntax",
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
        "can destroy local changes",
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
    # specific before general: "recursive permission change" must beat "permission change"
    (
        "recursive permission change",
        "Recursively changes permissions or ownership across a directory tree.",
    ),
    (
        "can modify files",
        "Runs find with -delete/-exec, which can modify or remove matched files.",
    ),
    (
        "credential/secret path",
        "Touches a path that looks like a credential or secret file.",
    ),
    (
        "outside the workspace boundary",
        "Writes to a path outside the workspace directory.",
    ),
    # -- MEDIUM families ------------------------------------------------------
    (
        "deletes a file",
        "Deletes a file in the workspace.",
    ),
    (
        "git push publishes commits",
        "Publishes local commits to the remote.",
    ),
    (
        "changes repository state",
        "Changes git repository state.",
    ),
    (
        "permission change",
        "Changes file permissions or ownership.",
    ),
    (
        "package operation",
        "Installs or modifies packages, which can run setup code and change your environment.",
    ),
    (
        "performs network activity",
        "Performs network activity (sends or fetches data over the network).",
    ),
    (
        "writes to the workspace",
        "Writes to files in the workspace.",
    ),
    (
        "signals a process",
        "Sends a signal to another process.",
    ),
    (
        "runs arbitrary python code",
        "Executes arbitrary Python code.",
    ),
    (
        "defaulting to medium",
        "Runs an unrecognized executable; its effects can't be determined in advance.",
    ),
    (
        "sensitive path",
        "Reads a file that looks like a credential or secret.",
    ),
)


def explain_risk(reasons: tuple[str, ...]) -> str:
    """Map classifier ``reasons`` to a fuller, one-line consequence explanation.

    For each reason, the first template whose needle is a substring of the
    reason contributes its sentence. Sentences are deduped (order preserved) and
    joined with a single space. Empty input, or input where no reason matches any
    needle, returns :data:`FALLBACK`.
    """
    sentences: list[str] = []
    for reason in reasons:
        for needle, sentence in _TEMPLATES:
            if needle in reason:
                if sentence not in sentences:
                    sentences.append(sentence)
                break
    if not sentences:
        return FALLBACK
    return " ".join(sentences)
