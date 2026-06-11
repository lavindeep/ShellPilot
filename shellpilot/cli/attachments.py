"""Image attachment staging for the /attach slash command (B9)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from shellpilot.llm.messages import ImageRef

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10 MiB


class AttachmentError(Exception):
    """Raised when an image file cannot be loaded for attachment."""


def load_image(path: Path) -> ImageRef:
    """Read an image file and return an ImageRef.

    Raises AttachmentError when:
    - The path is not a regular file.
    - The file extension is not in IMAGE_EXTENSIONS (case-insensitive).
    - The file size exceeds MAX_IMAGE_BYTES.
    """
    if not path.is_file():
        raise AttachmentError(f"not a file: {path}")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise AttachmentError(
            f"unsupported extension '{path.suffix}' — "
            f"supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )
    raw = path.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise AttachmentError(
            f"file size {len(raw):,} bytes exceeds the {MAX_IMAGE_BYTES:,}-byte size limit"
        )
    sha256 = hashlib.sha256(raw).hexdigest()
    data_b64 = base64.b64encode(raw).decode()
    return ImageRef(path=str(path), sha256=sha256, data_b64=data_b64)


@dataclass
class AttachmentQueue:
    """Holds image paths staged for the next user message.

    Paths are stored here (not bytes) so that if the user edits the file
    between /attach and sending the message, the final file contents are read.
    """

    paths: list[Path] = field(default_factory=list)

    def stage(self, path: Path) -> None:
        """Add a path to the queue."""
        self.paths.append(path)

    def take(self) -> list[Path]:
        """Return all staged paths and clear the queue."""
        result = list(self.paths)
        self.paths.clear()
        return result
