"""Tests for the DuckDuckGo search provider (shellpilot.web.search)."""

from __future__ import annotations

import httpx
import pytest

from shellpilot.web.errors import WebSearchError
from shellpilot.web.search import DuckDuckGoProvider, SearchResult

# ---------------------------------------------------------------------------
# Fixture HTML: 3 results with varied scenarios
# ---------------------------------------------------------------------------

_DDG_HTML = """\
<html><body>
<div class="results">
  <!-- Result 1: DDG redirect href -->
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc">
    Example Page
  </a>
  <span class="result__snippet">A snippet for result one.</span>

  <!-- Result 2: DDG redirect href, multi-class snippet element -->
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&rut=xyz">
    Python Home
  </a>
  <span class="web-result__snippet result__snippet extra-class">Python programming language.</span>

  <!-- Result 3: absolute http href, no snippet -->
  <a class="result__a" href="https://docs.python.org/3/">
    Python Docs
  </a>
</div>
</body></html>
"""

# A 200 response fixture with zero results
_DDG_EMPTY_HTML = "<html><body><p>No results found.</p></body></html>"


def _make_transport(html: str, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, html=html)

    return httpx.MockTransport(handler)


def _make_error_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parses_titles_urls_snippets() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport(_DDG_HTML))
    results = provider.search("python", max_results=5)

    assert len(results) == 3
    assert all(isinstance(r, SearchResult) for r in results)

    assert results[0].title == "Example Page"
    assert results[0].snippet == "A snippet for result one."

    assert results[1].title == "Python Home"
    assert results[1].snippet == "Python programming language."

    assert results[2].title == "Python Docs"
    assert results[2].snippet == ""


def test_decodes_uddg_redirect_urls() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport(_DDG_HTML))
    results = provider.search("python")

    assert results[0].url == "https://example.com/page"
    assert results[1].url == "https://www.python.org/"


def test_keeps_absolute_urls() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport(_DDG_HTML))
    results = provider.search("python")

    assert results[2].url == "https://docs.python.org/3/"


def test_respects_max_results() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport(_DDG_HTML))
    results = provider.search("python", max_results=2)

    assert len(results) == 2


def test_http_error_raises_search_error() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport("", status=503))
    with pytest.raises(WebSearchError):
        provider.search("python")


def test_transport_error_raises_search_error() -> None:
    provider = DuckDuckGoProvider(transport=_make_error_transport())
    with pytest.raises(WebSearchError):
        provider.search("python")


def test_zero_results_returns_empty_list() -> None:
    provider = DuckDuckGoProvider(transport=_make_transport(_DDG_EMPTY_HTML))
    results = provider.search("python")

    assert results == []


def test_sends_user_agent_and_query() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, html=_DDG_EMPTY_HTML)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))
    provider.search("shellpilot web")

    assert len(captured) == 1
    req = captured[0]

    ua = req.headers.get("user-agent", "")
    assert len(ua) > 20, "User-Agent should be a realistic browser-like string"
    assert any(token in ua for token in ("Mozilla", "Chrome", "Safari", "Firefox", "Gecko")), (
        f"UA looks non-browser-like: {ua!r}"
    )

    assert req.url.params.get("q") == "shellpilot web"
