"""Exception hierarchy for web grounding failures."""

from __future__ import annotations


class WebError(Exception):
    """Base exception for all web grounding failures."""


class WebSearchError(WebError):
    """Raised when a web search request fails."""


class WebFetchError(WebError):
    """Raised when fetching or processing a web page fails."""
