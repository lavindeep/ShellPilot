"""Secret redaction for logs and summaries (design sections 15, 24.7).

Deliberately pattern-based and conservative: obvious credentials are masked,
ordinary text is left alone. False negatives are possible; treat the audit log
as sensitive regardless.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Dict keys whose scalar values are secrets regardless of value shape. Compared
# case-insensitively with "-" normalised to "_" (so "API-Key" matches "api_key").
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "accesskey",
        "client_secret",
        "private_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "session_token",
        "aws_secret_access_key",
        "bearer_token",
    }
)


def _is_sensitive_key(key: str) -> bool:
    return key.lower().replace("-", "_") in _SENSITIVE_KEYS


def _is_redactable_scalar(item: object) -> bool:
    # Mask string and numeric scalars under a sensitive key. bool is a subclass
    # of int, so True/False (and None) are deliberately left intact.
    return isinstance(item, str) or (isinstance(item, (int, float)) and not isinstance(item, bool))


_PATTERNS: list[re.Pattern[str]] = [
    # AWS access key ids and secret-looking assignments
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub tokens (classic and fine-grained)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # JWTs
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # Bearer headers
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # key=value style credentials (allow prefixes like DB_PASSWORD, MY_API_KEY).
    # The optional quote before the separator also matches JSON keys ("api_key":).
    re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key)"
        r"['\"]?\s*[=:]\s*['\"]?[^\s'\"]{6,}['\"]?"
    ),
    # PEM private key blocks
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
]


def redact_secrets(text: str) -> str:
    """Mask obvious secrets; returns the text unchanged when nothing matches."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_structure(value: object) -> object:
    """Recursively redact secrets from arbitrarily nested str/dict/list values.

    String and numeric scalars (int/float, excluding bool) under a sensitive
    dict key are masked outright; everything else recurses, so non-string
    scalars (bool, None, …) outside a sensitive key pass through unchanged.
    Callers decide whether redaction is enabled; this function always redacts.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and _is_sensitive_key(key) and _is_redactable_scalar(item)
            else redact_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value
