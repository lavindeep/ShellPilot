"""Stdlib-only HTML-to-text extractor for web grounding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Tags whose entire subtree (including nested content) is suppressed.
_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form"}
)

# Block-level tags that should emit a newline on start and end.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "pre", "div", "section", "article"}
)


@dataclass(frozen=True)
class ExtractedPage:
    """Result of extracting readable text from an HTML document."""

    title: str
    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    """HTMLParser subclass that collects visible text from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth: int = 0
        self._in_title: bool = False
        self._title_parts: list[str] = []
        self._title_done: bool = False

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth > 0:
            if tag in _SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title" and not self._title_done:
            self._in_title = True
            return
        if tag == "br":
            self._chunks.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth > 0:
            if tag in _SKIP_TAGS:
                self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            self._title_done = True
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._chunks.append(data)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    def raw_text(self) -> str:
        return "".join(self._chunks)


def _clean(raw: str) -> str:
    """Collapse whitespace within lines and fold excessive blank lines."""
    lines: list[str] = []
    for line in raw.splitlines():
        # Collapse runs of spaces/tabs within each line.
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)

    # Fold 2+ consecutive blank lines down to one.
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append(line)
        else:
            blank_run = 0
            cleaned.append(line)

    return "\n".join(cleaned).strip()


def extract_text(html: str, *, max_chars: int = 20_000) -> ExtractedPage:
    """Extract readable text and title from an HTML string.

    Uses stdlib ``html.parser`` only — no third-party dependencies.  Skips
    non-content tags (script, style, nav, etc.), emits newlines at block
    boundaries, collapses whitespace, and caps output at *max_chars*.

    Args:
        html: Raw HTML source.
        max_chars: Maximum characters of body text to return.  Defaults to
            20 000.  When the cleaned text exceeds this limit it is truncated
            and ``ExtractedPage.truncated`` is set to ``True``.

    Returns:
        An :class:`ExtractedPage` with ``title``, ``text``, and ``truncated``.
    """
    parser = _TextExtractor()
    parser.feed(html)

    text = _clean(parser.raw_text())
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    return ExtractedPage(title=parser.title, text=text, truncated=truncated)
