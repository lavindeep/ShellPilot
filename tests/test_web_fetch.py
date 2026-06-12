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
    """FetchedPage.url must reflect the final URL after following a redirect hop."""
    final_html = "<html><body><p>Landed</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            # 302 → /b on the same host
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/b",
                    "content-type": "text/html",
                },
                content=b"",
            )
        # /b: serve the final page
        return httpx.Response(
            200,
            content=final_html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))
    page = fetcher.fetch("https://example.com/a")

    assert page.url.endswith("/b"), f"Expected final URL to end with /b, got {page.url!r}"
    assert "Landed" in page.text


def test_redirect_to_private_host_blocked() -> None:
    """A public URL that redirects to a private IP must be blocked before the second request."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "example.com":
            # 302 → private IP
            return httpx.Response(
                302,
                headers={
                    "location": "http://127.0.0.1/secret",
                    "content-type": "text/html",
                },
                content=b"",
            )
        # Should never be reached
        return httpx.Response(200, content=b"secret", headers={"content-type": "text/plain"})

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    with pytest.raises(WebFetchError):
        fetcher.fetch("http://example.com/a")

    # Only the first request (to example.com) should have been made;
    # the private-IP hop must be blocked before any connection attempt.
    assert len(calls) == 1, (
        f"Expected exactly 1 transport call, got {len(calls)}: {[str(r.url) for r in calls]}"
    )


def test_too_many_redirects() -> None:
    """A redirect loop must raise WebFetchError after MAX_REDIRECTS hops."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Always redirect back to /loop
        return httpx.Response(
            302,
            headers={
                "location": "https://example.com/loop",
                "content-type": "text/html",
            },
            content=b"",
        )

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    with pytest.raises(WebFetchError, match="too many redirects"):
        fetcher.fetch("https://example.com/loop")


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


def test_rejects_localhost_subdomains() -> None:
    """*.localhost subdomains must be blocked pre-request."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html="<p>leak</p>")

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    blocked_urls = [
        "http://foo.localhost/",
        "http://a.b.localhost:8080/path",
    ]
    for url in blocked_urls:
        with pytest.raises(WebFetchError):
            fetcher.fetch(url)

    assert calls == [], f"Transport called for *.localhost URLs: {[r.url for r in calls]}"


def test_rejects_legacy_short_dotted_loopback() -> None:
    """Legacy short-dotted numeric forms that expand to loopback must be blocked."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html="<p>leak</p>")

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    blocked_urls = [
        "http://127.1/",
        "http://127.0.1/",
    ]
    for url in blocked_urls:
        with pytest.raises(WebFetchError):
            fetcher.fetch(url)

    assert calls == [], f"Transport called for legacy-numeric URLs: {[r.url for r in calls]}"


def test_rejects_numeric_alternate_encodings() -> None:
    """Decimal-int and hex-encoded loopback IPs must remain blocked (regression)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html="<p>leak</p>")

    fetcher = PageFetcher(transport=httpx.MockTransport(handler))

    blocked_urls = [
        "http://2130706433/",  # 127.0.0.1 as decimal int
        "http://0x7f000001/",  # 127.0.0.1 as hex
        "http://[::1]/",  # IPv6 loopback
        "http://localhost/",  # name
        "http://10.0.0.5/",  # RFC-1918 private
        "http://192.168.1.1/",  # RFC-1918 private
    ]
    for url in blocked_urls:
        with pytest.raises(WebFetchError):
            fetcher.fetch(url)

    assert calls == [], f"Transport was called for blocked URLs: {[r.url for r in calls]}"


def test_allows_normal_public_hosts() -> None:
    """Public hostnames must pass the URL guard (no network involved)."""
    from shellpilot.web.fetch import _check_url

    # These must not raise
    _check_url("https://example.com/")
    _check_url("https://pypi.org/simple/")
    _check_url("http://example.org/page")


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


def test_charset_latin1_decoded_correctly() -> None:
    """Response with content-type charset=latin-1 must decode latin-1 bytes correctly."""
    # b"\xe9" is 'é' in latin-1
    body = b"caf\xe9"
    transport = _bytes_transport(body, content_type="text/plain; charset=latin-1")
    fetcher = PageFetcher(transport=transport)

    page = fetcher.fetch("https://example.com/text")
    assert "café" in page.text


# ---------------------------------------------------------------------------
# Trailing-dot hostname normalisation (v0.5.2)
# ---------------------------------------------------------------------------


def test_rejects_trailing_dot_localhost() -> None:
    """http://localhost./ must be blocked (trailing dot is a valid FQDN root indicator)."""
    from shellpilot.web.fetch import _check_url

    with pytest.raises(WebFetchError):
        _check_url("http://localhost./")


def test_rejects_trailing_dot_subdomain_localhost() -> None:
    """http://foo.localhost./ must be blocked."""
    from shellpilot.web.fetch import _check_url

    with pytest.raises(WebFetchError):
        _check_url("http://foo.localhost./")


def test_rejects_trailing_dot_loopback_name() -> None:
    """http://0.0.0.0./ must be blocked."""
    from shellpilot.web.fetch import _check_url

    with pytest.raises(WebFetchError):
        _check_url("http://0.0.0.0./")


def test_rejects_trailing_dot_loopback_ip() -> None:
    """http://127.0.0.1./ must be blocked — inet_aton bypass via trailing dot."""
    from shellpilot.web.fetch import _check_url

    with pytest.raises(WebFetchError):
        _check_url("http://127.0.0.1./")


def test_rejects_multi_trailing_dots() -> None:
    """http://localhost../ (multiple trailing dots) must be blocked."""
    from shellpilot.web.fetch import _check_url

    with pytest.raises(WebFetchError):
        _check_url("http://localhost../")


def test_allows_public_domain_with_trailing_dot() -> None:
    """http://example.com./ (FQDN with trailing dot) must still be allowed."""
    from shellpilot.web.fetch import _check_url

    # Must not raise — public domain, trailing dot is a valid DNS encoding
    _check_url("http://example.com./")


def test_existing_blocks_unaffected_by_trailing_dot_change() -> None:
    """Regression: existing blocks (private IPs, legacy forms) remain green."""
    from shellpilot.web.fetch import _check_url

    blocked = [
        "http://localhost/",
        "http://foo.localhost/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
    ]
    for url in blocked:
        with pytest.raises(WebFetchError):
            _check_url(url)
