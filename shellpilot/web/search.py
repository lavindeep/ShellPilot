"""DuckDuckGo no-key search provider for opt-in web grounding.

This module implements a ``SearchProvider`` protocol and a concrete
``DuckDuckGoProvider`` that hits the public DDG HTML endpoint.  The provider
seam is intentional: any object implementing ``SearchProvider.search`` can be
swapped in (e.g., for tests or alternative engines) when passed to the web
tools factory — a later task wires that up.  There is no plugin registry by
design; the concrete type is selected at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from shellpilot.web.errors import WebSearchError

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# A realistic desktop User-Agent string.  DDG rejects requests with an empty
# or bot-like UA, so we identify as a recent Chrome on macOS.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SearchResult:
    """A single result returned by a search provider."""

    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    """Minimal interface that all search providers must satisfy."""

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Execute *query* and return up to *max_results* results."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# HTML parser for DDG's result page
# ---------------------------------------------------------------------------


def _has_class(attrs: list[tuple[str, str | None]], cls: str) -> bool:
    """Return True when any attribute is ``class`` and contains *cls* as a token."""
    for name, value in attrs:
        if name == "class" and value is not None:
            if cls in value.split():
                return True
    return False


class _DDGParser(HTMLParser):
    """Parse DuckDuckGo HTML results into ``SearchResult`` objects."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Pending state — we build one result at a time.
        self._in_link: bool = False
        self._current_href: str = ""
        self._current_title_parts: list[str] = []
        # Pending (href, title) waiting to be paired with a snippet.
        self._pending: tuple[str, str] | None = None
        # Snippet accumulation
        self._in_snippet: bool = False
        self._snippet_depth: int = 0
        self._current_snippet_parts: list[str] = []
        # Completed results
        self.results: list[tuple[str, str, str]] = []  # (title, url, snippet)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._in_snippet:
            self._snippet_depth += 1
            return
        if not self._in_link and tag == "a" and _has_class(attrs, "result__a"):
            href = next((v for k, v in attrs if k == "href" and v is not None), "")
            self._in_link = True
            self._current_href = href
            self._current_title_parts = []
            return
        if self._pending is not None and _has_class(attrs, "result__snippet"):
            self._in_snippet = True
            self._snippet_depth = 1
            self._current_snippet_parts = []
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_snippet:
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                self._in_snippet = False
                snippet = "".join(self._current_snippet_parts).strip()
                self._current_snippet_parts = []
                if self._pending is not None:
                    title, url = self._pending
                    self._pending = None
                    self.results.append((title, url, snippet))
            return
        if self._in_link and tag == "a":
            self._in_link = False
            title = "".join(self._current_title_parts).strip()
            url = _resolve_url(self._current_href)
            if url:
                # Flush any previous pending without snippet
                if self._pending is not None:
                    prev_title, prev_url = self._pending
                    self.results.append((prev_title, prev_url, ""))
                self._pending = (title, url)

    def handle_data(self, data: str) -> None:
        if self._in_snippet:
            self._current_snippet_parts.append(data)
        elif self._in_link:
            self._current_title_parts.append(data)

    def flush_pending(self) -> None:
        """Emit any result still waiting for a snippet as snippet=""."""
        if self._pending is not None:
            title, url = self._pending
            self.results.append((title, url, ""))
            self._pending = None


# ---------------------------------------------------------------------------
# URL resolution helper
# ---------------------------------------------------------------------------


def _resolve_url(href: str) -> str:
    """Turn a DDG href into the canonical destination URL.

    DDG result links come in two forms:
    - ``//duckduckgo.com/l/?uddg=<urlencoded-target>&rut=...``
      → decode and return the ``uddg`` value.
    - An absolute http(s) URL  → return as-is.
    - Anything else            → return "" (will be skipped).
    """
    # Normalise protocol-relative DDG redirect links
    if href.startswith("//"):
        href = "https:" + href

    parts = urlsplit(href)
    if parts.scheme not in ("http", "https"):
        return ""

    # DDG redirect URL
    if parts.hostname in ("duckduckgo.com", "www.duckduckgo.com") and parts.path == "/l/":
        qs = parse_qs(parts.query)
        uddg = qs.get("uddg", [])
        if uddg:
            target = uddg[0]
            return target if urlsplit(target).scheme in ("http", "https") else ""
        return ""

    # Already an absolute http(s) link
    return href


# ---------------------------------------------------------------------------
# Public provider
# ---------------------------------------------------------------------------


class DuckDuckGoProvider:
    """Search provider that queries the DuckDuckGo HTML endpoint.

    No API key required.  Implements :class:`SearchProvider`.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": _USER_AGENT},
            transport=transport,
            trust_env=False,
        )

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Query DuckDuckGo and return up to *max_results* results.

        Raises:
            WebSearchError: On transport failures or HTTP 4xx/5xx responses.
        """
        try:
            response = self._client.get(_DDG_HTML_URL, params={"q": query})
        except httpx.TransportError as exc:
            raise WebSearchError(f"DuckDuckGo request failed: {exc}") from exc

        if response.status_code >= 400:
            raise WebSearchError(f"DuckDuckGo returned HTTP {response.status_code}")

        parser = _DDGParser()
        parser.feed(response.text)
        parser.flush_pending()

        results: list[SearchResult] = [
            SearchResult(title=title, url=url, snippet=snippet)
            for title, url, snippet in parser.results
        ]
        return results[:max_results]

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()
