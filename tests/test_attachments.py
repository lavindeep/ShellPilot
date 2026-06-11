"""Tests for shellpilot/cli/attachments.py (B9: /attach staging)."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from tests.conftest import TINY_PNG

# ---------------------------------------------------------------------------
# load_image tests
# ---------------------------------------------------------------------------


def test_load_image_round_trips_tiny_png(tmp_path: Path) -> None:
    """load_image encodes TINY_PNG bytes into base64 and sha256 that round-trip."""
    from shellpilot.cli.attachments import load_image

    img_file = tmp_path / "sample.png"
    img_file.write_bytes(TINY_PNG)

    ref = load_image(img_file)

    assert ref.path == str(img_file)
    assert ref.sha256 == hashlib.sha256(TINY_PNG).hexdigest()
    assert base64.b64decode(ref.data_b64) == TINY_PNG


def test_load_image_rejects_bad_extension(tmp_path: Path) -> None:
    """load_image raises AttachmentError for unsupported file extensions."""
    from shellpilot.cli.attachments import AttachmentError, load_image

    bad_file = tmp_path / "document.pdf"
    bad_file.write_bytes(b"%PDF fake")

    with pytest.raises(AttachmentError, match="extension"):
        load_image(bad_file)


def test_load_image_rejects_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_image raises AttachmentError when the file exceeds MAX_IMAGE_BYTES."""
    import shellpilot.cli.attachments as att_mod
    from shellpilot.cli.attachments import AttachmentError, load_image

    monkeypatch.setattr(att_mod, "MAX_IMAGE_BYTES", 10)

    big_file = tmp_path / "big.png"
    big_file.write_bytes(b"\x89PNG" + b"x" * 20)  # 24 bytes > 10

    with pytest.raises(AttachmentError, match="size"):
        load_image(big_file)


def test_load_image_rejects_missing_file(tmp_path: Path) -> None:
    """load_image raises AttachmentError when the file does not exist."""
    from shellpilot.cli.attachments import AttachmentError, load_image

    with pytest.raises(AttachmentError, match="not a file"):
        load_image(tmp_path / "ghost.png")


# ---------------------------------------------------------------------------
# AttachmentQueue tests
# ---------------------------------------------------------------------------


def test_attachment_queue_take_clears(tmp_path: Path) -> None:
    """AttachmentQueue.take() returns staged paths and clears the queue."""
    from shellpilot.cli.attachments import AttachmentQueue

    q: AttachmentQueue = AttachmentQueue()
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.jpg"
    q.stage(p1)
    q.stage(p2)

    result = q.take()

    assert result == [p1, p2]
    assert q.take() == []  # cleared after first take
