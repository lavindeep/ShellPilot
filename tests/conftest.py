"""Shared pytest fixtures and constants for the ShellPilot test suite."""

from __future__ import annotations

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
