"""Bounded HTTP page fetcher with scheme and private-host guards.

Fetches a URL, enforces conservative pre-request guards, caps the download
size, decodes the body, and extracts readable text (HTML) or passes through
plain text, returning a :class:`FetchedPage`.

Redirect handling
-----------------
Redirects are followed manually (up to :data:`MAX_REDIRECTS` hops).  Each
redirect destination is passed through :func:`_check_url` before the next
request is issued, so a public URL that 302s to a private IP is blocked at
the second hop before any connection is made.

Known limitation — DNS-based private-IP bypass
----------------------------------------------
Guards are applied to the URL hostname before any network request.  If a
public-looking DNS name resolves to a private/loopback IP (DNS rebinding), the
fetcher will not catch it.  DNS pinning or post-connect IP inspection is not
implemented; the guards here cover the common/accidental cases, not adversarial
ones.  Users who need stronger SSRF protection should layer this behind a
network-level egress filter or a dedicated DNS-pinning proxy.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from shellpilot.web.errors import WebFetchError
from shellpilot.web.extract import extract_text

# Content-type tokens that we accept (case-insensitive match).
_ACCEPTED_CONTENT_TYPES = ("text/html", "text/plain", "xml")

# Matches hosts that look numeric (digits, hex digits, dots) — used to detect
# legacy short-dotted IPv4 forms such as "127.1" before attempting inet_aton.
_NUMERIC_HOST_RE = re.compile(r"^[0-9a-fA-F.x]+$")

# Maximum number of redirect hops before giving up.
MAX_REDIRECTS = 10


@dataclass(frozen=True)
class FetchedPage:
    """Result of fetching and extracting a web page."""

    url: str  # Final URL after redirects
    title: str  # Document title (empty for plain text)
    text: str  # Extracted/truncated body text
    truncated: bool  # True when either byte cap or char cap was hit


def _check_url(url: str) -> None:
    """Raise :class:`WebFetchError` if *url* fails pre-request guards.

    Checks (in order):
    1. Scheme must be ``http`` or ``https``.
    2. Hostname must not be empty.
    3. Literal IP addresses must not be loopback / private / link-local /
       reserved.  The hostname ``localhost`` and ``0.0.0.0`` are also blocked.
    """
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https"):
        raise WebFetchError(
            f"Unsupported URL scheme {parts.scheme!r}; only http and https are allowed."
        )

    hostname = (parts.hostname or "").rstrip(".")
    if not hostname:
        raise WebFetchError("URL has an empty hostname.")

    # Block by name first (exact names and *.localhost subdomains)
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname == "0.0.0.0":
        raise WebFetchError(f"Fetching {hostname!r} is not allowed (blocked hostname).")

    # Try to parse as an IP literal (strip IPv6 brackets if present)
    ip_str = hostname
    if ip_str.startswith("[") and ip_str.endswith("]"):
        ip_str = ip_str[1:-1]
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not a standard IP literal.  If the host looks purely numeric it may
        # be a legacy short-dotted form (e.g. "127.1") that ipaddress rejects
        # but some resolver stacks expand to a full IPv4 address.  Try
        # inet_aton as a fallback parser; on success, validate the resulting
        # address through the same checks used for standard IP literals.
        if _NUMERIC_HOST_RE.fullmatch(ip_str):
            try:
                packed = socket.inet_aton(ip_str)
                addr = ipaddress.IPv4Address(int.from_bytes(packed, "big"))
            except OSError:
                # Not parseable by inet_aton either — treat as DNS name.
                return
        else:
            # DNS name; DNS resolution is NOT checked here.
            return

    if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
        raise WebFetchError(
            f"Fetching IP address {addr!s} is not allowed (loopback/private/link-local/reserved)."
        )


def _content_type_accepted(content_type: str) -> bool:
    """Return True when the Content-Type contains an accepted token."""
    ct_lower = content_type.lower()
    return any(token in ct_lower for token in _ACCEPTED_CONTENT_TYPES)


class PageFetcher:
    """Fetch a single web page with conservative size and host guards.

    Mirrors the httpx patterns of ``shellpilot.llm.ollama.OllamaClient``:
    explicit ``httpx.Timeout``, injectable ``transport`` for tests, narrow
    exception wrapping.

    Redirects are followed manually so that every hop's destination URL is
    checked against :func:`_check_url` before a connection is made.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 20.0,
        max_bytes: int = 2_000_000,
        max_chars: int = 20_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, read=read_timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._max_bytes = max_bytes
        self._max_chars = max_chars

    def fetch(self, url: str) -> FetchedPage:
        """Fetch *url* and return a :class:`FetchedPage`.

        Redirects are followed manually (up to :data:`MAX_REDIRECTS` hops).
        Each redirect destination is validated by :func:`_check_url` before the
        next request is issued.

        Raises:
            WebFetchError: For scheme/host guard failures, bad content types,
                HTTP 4xx/5xx responses, too many redirects, or transport errors.
        """
        # Pre-request guards — no network access happens before this passes.
        _check_url(url)

        current_url = url
        hops = 0

        try:
            while True:
                with self._client.stream("GET", current_url) as response:
                    # Follow redirects manually so we can guard each hop.
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise WebFetchError(
                                f"Redirect from {current_url!r} has no Location header."
                            )
                        next_url = urljoin(current_url, location)
                        hops += 1
                        if hops >= MAX_REDIRECTS:
                            raise WebFetchError(
                                f"too many redirects following {url!r} "
                                f"(exceeded {MAX_REDIRECTS} hops)"
                            )
                        # Guard the redirect destination BEFORE requesting it.
                        _check_url(next_url)
                        current_url = next_url
                        continue

                    if response.status_code >= 400:
                        raise WebFetchError(f"HTTP {response.status_code} fetching {current_url}")

                    content_type = response.headers.get("content-type", "")
                    if not _content_type_accepted(content_type):
                        raise WebFetchError(
                            f"Unsupported content type {content_type!r}; "
                            f"expected text/html, text/plain, or XML."
                        )

                    # Stream body up to max_bytes
                    chunks: list[bytes] = []
                    bytes_read = 0
                    byte_truncated = False
                    for chunk in response.iter_bytes():
                        remaining = self._max_bytes - bytes_read
                        if len(chunk) >= remaining:
                            chunks.append(chunk[:remaining])
                            byte_truncated = True
                            break
                        chunks.append(chunk)
                        bytes_read += len(chunk)

                    raw_bytes = b"".join(chunks)

                    # Determine charset
                    charset: str = "utf-8"
                    if response.encoding:
                        charset = response.encoding

                    body = raw_bytes.decode(charset, errors="replace")
                    final_url = str(response.url)
                    break  # Successful non-redirect response

        except WebFetchError:
            raise
        except httpx.TransportError as exc:
            raise WebFetchError(f"Transport error fetching {current_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise WebFetchError(f"HTTP error fetching {current_url}: {exc}") from exc

        # Extract text based on content type
        ct_lower = content_type.lower()
        if "text/plain" in ct_lower:
            # Pass through plain text with char cap
            if len(body) > self._max_chars:
                text = body[: self._max_chars]
                truncated = True
            else:
                text = body
                truncated = byte_truncated
            return FetchedPage(url=final_url, title="", text=text, truncated=truncated)

        # HTML / XML path
        extracted = extract_text(body, max_chars=self._max_chars)
        truncated = byte_truncated or extracted.truncated
        return FetchedPage(
            url=final_url,
            title=extracted.title,
            text=extracted.text,
            truncated=truncated,
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()
