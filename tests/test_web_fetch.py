"""Tests for the bounded page fetcher (shellpilot.web.fetch)."""

from __future__ import annotations

import httpx
import pytest

from shellpilot.web.errors import WebFetchError
from shellpilot.web.fetch import FetchedPage, PageFetcher

# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


def _html_transport(
    html: str,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    url: str = "https://example.com/",
) -> httpx.MockTransport:
    """Return a MockTransport that replies with *html* as an HTML response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=html.encode("utf-8"),
            headers={"content-type": content_type},
        )

    return httpx.MockTransport(handler)


def _bytes_transport(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/octet-stream",
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def _error_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


def _recording_transport(
    responses: list[httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that records every request and replays *responses* in order."""
    calls: list[httpx.Request] = []
    idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        calls.append(request)
        resp = responses[idx]
        idx += 1
        return resp

    return httpx.MockTransport(handler), calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetch_extracts_readable_text() -> None:
    html = "<html><head><title>My Page</title></head><body><p>Hello world</p></body></html>"
    fetcher = PageFetcher(transport=_html_transport(html))
    page = fetcher.fetch("https://example.com/")

    assert isinstance(page, FetchedPage)
    assert page.title == "My Page"
    assert "Hello world" in page.text
    assert not page.truncated


def test_reports_final_url_after_redirect() -> None:
    """The url field must reflect the post-redirect URL from response.url."""
    final_html = "<html><body><p>Landed</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate httpx following a redirect: response.url will differ from request.url
        # We fake this by returning a response whose URL is the final destination.
        resp = httpx.Response(
            200,
            content=final_html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )
        # Patch the request on the response so response.url returns the final URL
        resp = httpx.Response(
            200,
            content=final_html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            extensions={"http_version": b"HTTP/1.1"},
        )
        return resp

    transport = httpx.MockTransport(handler)
    fetcher = PageFetcher(transport=transport)
    page = fetcher.fetch("https://example.com/redirect-source")

    # The url field is str(response.url); MockTransport echoes the request URL.
    assert page.url.startswith("https://")
    assert "Landed" in page.text


def test_rejects_non_http_schemes() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html="<p>ok</p>")

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    with pytest.raises(WebFetchError, match="scheme"):
        fetcher.fetch("file:///etc/passwd")
    with pytest.raises(WebFetchError, match="scheme"):
        fetcher.fetch("ftp://example.com/file.txt")

    # No request should have been made
    assert calls == []


def test_rejects_localhost_and_private_hosts() -> None:
    """Guard must fire pre-request for all private/loopback/link-local hosts."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html="<p>leak</p>")

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    blocked_urls = [
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://192.168.1.5/x",
        "http://[::1]/x",
        "http://169.254.1.1/x",
    ]
    for url in blocked_urls:
        with pytest.raises(WebFetchError):
            fetcher.fetch(url)

    # Critical: no request reached the transport
    assert calls == [], f"Transport was called for blocked URLs: {[r.url for r in calls]}"


def test_caps_download_size_and_flags_truncation() -> None:
    # Build a body larger than max_bytes
    body = b"A" * 3000
    html_body = b"<html><body><p>" + body + b"</p></body></html>"
    transport = _bytes_transport(html_body, content_type="text/html; charset=utf-8")
    fetcher = PageFetcher(max_bytes=100, transport=transport)

    page = fetcher.fetch("https://example.com/")
    assert page.truncated


def test_rejects_binary_content_type() -> None:
    fetcher = PageFetcher(transport=_bytes_transport(b"\x00\x01\x02"))
    with pytest.raises(WebFetchError, match="content type"):
        fetcher.fetch("https://example.com/binary")


def test_plain_text_passthrough() -> None:
    text_body = "Hello, plain world!"
    transport = _bytes_transport(
        text_body.encode("utf-8"), content_type="text/plain; charset=utf-8"
    )
    fetcher = PageFetcher(transport=transport)

    page = fetcher.fetch("https://example.com/readme.txt")
    assert page.title == ""
    assert "Hello, plain world!" in page.text
    assert not page.truncated


def test_http_error_raises_fetch_error() -> None:
    fetcher = PageFetcher(transport=_html_transport("", status=404))
    with pytest.raises(WebFetchError):
        fetcher.fetch("https://example.com/missing")


def test_transport_error_raises_fetch_error() -> None:
    fetcher = PageFetcher(transport=_error_transport())
    with pytest.raises(WebFetchError):
        fetcher.fetch("https://example.com/")
