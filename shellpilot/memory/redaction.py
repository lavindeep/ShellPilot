"""Secret redaction for logs and summaries (design sections 15, 24.7).

Deliberately pattern-based and conservative: obvious credentials are masked,
ordinary text is left alone. False negatives are possible; treat the audit log
as sensitive regardless.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

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
    # key=value style credentials (allow prefixes like DB_PASSWORD, MY_API_KEY)
    re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key)"
        r"\s*[=:]\s*['\"]?[^\s'\"]{6,}['\"]?"
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

    Non-string scalars (int, float, bool, None, …) pass through unchanged.
    Callers decide whether redaction is enabled; this function always redacts.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value
