"""Tests for secret redaction (design section 15)."""

from shellpilot.memory.redaction import REDACTED, redact_secrets, redact_structure


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


def test_structure_redacts_value_under_sensitive_key() -> None:
    # The value alone matches no value-pattern; only the key marks it secret.
    assert redact_structure({"api_key": "1234567890"}) == {"api_key": REDACTED}


def test_structure_redacts_common_sensitive_keys() -> None:
    for key in ("password", "token", "secret"):
        assert redact_structure({key: "supersecretvalue"}) == {key: REDACTED}


def test_structure_sensitive_key_normalizes_case_and_hyphen() -> None:
    assert redact_structure({"API-Key": "1234567890"}) == {"API-Key": REDACTED}


def test_json_shaped_secret_in_string_redacted() -> None:
    assert REDACTED in redact_secrets('"api_key": "1234567890"')


def test_structure_benign_dict_unchanged() -> None:
    benign = {"name": "alice", "count": 5}
    assert redact_structure(benign) == benign


def test_structure_container_under_sensitive_key_recurses() -> None:
    # Containers under a sensitive key are walked, not force-masked.
    assert redact_structure({"token": ["a", "b"]}) == {"token": ["a", "b"]}
    assert redact_structure({"secret": {"inner": "x"}}) == {"secret": {"inner": "x"}}


def test_structure_numeric_scalar_under_sensitive_key_redacted() -> None:
    assert redact_structure({"api_key": 1234567890}) == {"api_key": REDACTED}
    assert redact_structure({"api_key": 1234567890.5}) == {"api_key": REDACTED}


def test_structure_bool_and_none_under_sensitive_key_unchanged() -> None:
    # bool is a subclass of int; True/False and None must never be masked.
    assert redact_structure({"secret": True}) == {"secret": True}
    assert redact_structure({"token": None}) == {"token": None}
    assert redact_structure({"flag": True}) == {"flag": True}
    assert redact_structure({"x": None}) == {"x": None}


def test_structure_redacts_expanded_token_keys() -> None:
    assert redact_structure({"access_token": "ya29.xxxxxxxxxx"}) == {"access_token": REDACTED}


def test_structure_nested_sensitive_key_redacted() -> None:
    nested = {"config": {"password": "hunter2pass"}}
    assert redact_structure(nested) == {"config": {"password": REDACTED}}


def test_benign_prose_not_redacted() -> None:
    assert redact_secrets("the api docs are here") == "the api docs are here"
    assert redact_secrets("time: now") == "time: now"
