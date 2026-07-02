"""Shared pytest fixtures and constants for the ShellPilot test suite."""

from __future__ import annotations

import pytest

# Ambient color overrides honored by rich (FORCE_COLOR, NO_COLOR, COLORTERM,
# TTY_COMPATIBLE, TTY_INTERACTIVE) and prompt_toolkit
# (PROMPT_TOOLKIT_COLOR_DEPTH, NO_COLOR). Any of these in the invoking shell
# flips Console.is_terminal / color-depth decisions and breaks tests that pin
# terminal vs non-terminal rendering — e.g. FORCE_COLOR=3 makes a StringIO
# console claim to be a terminal. The suite must not depend on who runs it.
COLOR_ENV_VARS = (
    "FORCE_COLOR",
    "NO_COLOR",
    "COLORTERM",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "PROMPT_TOOLKIT_COLOR_DEPTH",
)


@pytest.fixture(autouse=True)
def _hermetic_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient color env overrides so every test sees the same terminal."""
    for var in COLOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# Smallest valid 1×1 white RGB PNG (69 bytes), generated via zlib.compress + CRC.
TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a"  # PNG signature
    "0000000d49484452"  # IHDR chunk: length=13, type
    "00000001000000010802000000907753de"  # 1×1, 8-bit RGB, CRC
    "0000000c49444154789c63f8ffff3f0005fe02fe0def46b8"  # IDAT chunk
    "0000000049454e44ae426082"  # IEND chunk
)


def test_tiny_png_has_valid_signature() -> None:
    """TINY_PNG starts with the 8-byte PNG magic number."""
    assert TINY_PNG[:8] == b"\x89PNG\r\n\x1a\n"
