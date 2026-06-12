"""Table-driven tests for the deterministic command policy (design section 14.3)."""

from pathlib import Path

import pytest

from shellpilot.policy.command_policy import classify_command, sensitive_path_reason
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
    (["python", "-m", "pytest"], RiskLevel.LOW),
    (["pytest", "-q"], RiskLevel.LOW),
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


@pytest.mark.parametrize(("argv", "expected"), CASES, ids=lambda case: str(case))
def test_classification_table(argv: list[str], expected: RiskLevel) -> None:
    result = classify_command(argv, workspace=WS)
    assert result.risk == expected, f"{argv}: {result.reasons}"


def test_write_outside_workspace_is_high() -> None:
    result = classify_command(["mv", "a.txt", "/etc/hosts"], workspace=WS)
    assert result.risk == RiskLevel.HIGH
    assert any("outside" in reason for reason in result.reasons)


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
