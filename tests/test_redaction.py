"""Tests for secret redaction (design section 15)."""

from shellpilot.memory.redaction import REDACTED, redact_secrets


def test_aws_access_key_redacted() -> None:
    assert REDACTED in redact_secrets("key id AKIAIOSFODNN7EXAMPLE found")


def test_github_token_redacted() -> None:
    assert REDACTED in redact_secrets("token ghp_abcdefghijklmnopqrstuvwxyz0123456789")


def test_slack_token_redacted() -> None:
    assert REDACTED in redact_secrets("xoxb-123456789012-abcdefghij")


def test_jwt_redacted() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert REDACTED in redact_secrets(f"auth: {jwt}")


def test_bearer_header_redacted() -> None:
    assert REDACTED in redact_secrets("Authorization: Bearer abc123def456ghi789jkl0")


def test_password_assignment_redacted() -> None:
    assert REDACTED in redact_secrets("DB_PASSWORD=hunter2secret")
    assert REDACTED in redact_secrets('api_key: "sk-not-a-real-key-123456"')


def test_pem_block_redacted() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
    result = redact_secrets(pem)
    assert "MIIEow" not in result
    assert REDACTED in result


def test_ordinary_text_untouched() -> None:
    text = "Run pytest -q in the workspace; the token count is 42."
    assert redact_secrets(text) == text
