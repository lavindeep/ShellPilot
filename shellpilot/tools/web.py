"""Opt-in web grounding tools: web_search and web_fetch (design section 33).

These tools contact the public internet — every call goes through the standard
broker approval flow (SideEffect.NETWORK → Decision.ASK in every profile).
The tools are only registered in the runtime when ``[tools] web = true`` is
set in the config; they are never available by default.

Usage guidance baked into every tool description:
- These tools contact the internet; every call requires user approval.
- Prefer local project evidence first (read_file, search_text) before
  reaching out to the web.
- web_fetch only accepts http/https public hosts (no private IPs, no localhost).
"""

from __future__ import annotations

from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec
from shellpilot.web.errors import WebFetchError, WebSearchError
from shellpilot.web.fetch import PageFetcher
from shellpilot.web.search import SearchProvider


def make_web_tools(provider: SearchProvider, fetcher: PageFetcher) -> list[ToolSpec]:
    """Build web_search and web_fetch ToolSpecs backed by *provider* and *fetcher*.

    Inject mock implementations in tests; call :func:`default_web_tools` for
    production use.
    """

    def _search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(
                success=False,
                summary="query is required",
                content="Provide a non-empty query string.",
            )
        try:
            max_results = int(arguments.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 10))
        try:
            results = provider.search(query, max_results=max_results)
        except WebSearchError as exc:
            return ToolResult(success=False, summary=str(exc), content=str(exc))

        if not results:
            return ToolResult(
                success=True,
                summary=f"No results for {query!r}",
                content="No results.",
            )

        lines: list[str] = []
        for i, r in enumerate(results, start=1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   {r.url}")
            if r.snippet:
                lines.append(f"   {r.snippet}")
        content = "\n".join(lines)
        summary = f"{len(results)} result{'s' if len(results) != 1 else ''} for {query!r}"
        return ToolResult(success=True, summary=summary, content=content)

    def _fetch(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url", "")).strip()
        try:
            page = fetcher.fetch(url)
        except WebFetchError as exc:
            return ToolResult(success=False, summary=str(exc), content=str(exc))

        lines: list[str] = []
        if page.title:
            lines.append(f"Title: {page.title}")
        lines.append(f"URL: {page.url}")
        lines.append("")
        lines.append(page.text)
        if page.truncated:
            lines.append(
                "\n[content truncated — showing the first part of the page; "
                "fetch a more specific official URL (such as a releases, docs, "
                "changelog, pricing, or API reference page) if the needed fact "
                "is not visible]"
            )
        content = "\n".join(lines)
        summary = f"fetched {page.url}"
        if page.truncated:
            summary += " (truncated)"
        return ToolResult(
            success=True,
            summary=summary,
            content=content,
            truncated=page.truncated,
        )

    web_search = ToolSpec(
        definition=ToolDefinition(
            name="web_search",
            description=(
                "Search the web and return a numbered list of results "
                "(title, URL, snippet). "
                "IMPORTANT: this tool contacts the internet — every call requires "
                "individual user approval before it runs. "
                "Prefer local project evidence (read_file, search_text) before reaching "
                "out to the web. "
                "Results are leads, not evidence: a snippet is a preview, not the source. "
                "To ground a factual, current, or numeric claim, follow up with web_fetch "
                "on the most authoritative result and read the page itself."
            ),
            parameters={
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            required=("query",),
        ),
        side_effect=SideEffect.NETWORK,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=ALL_PROFILES,
        handler=_search,
    )

    web_fetch = ToolSpec(
        definition=ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a public web page and return its readable text content. "
                "IMPORTANT: this tool contacts the internet — every call requires "
                "individual user approval before it runs. "
                "Prefer local project evidence (read_file, search_text) before reaching "
                "out to the web. "
                "Only http/https public hosts are allowed; localhost, private IPs, and "
                "non-http schemes are blocked. "
                "Output format: 'Title: ...\nURL: <final url after redirects>\n\n<text>'."
            ),
            parameters={
                "url": {
                    "type": "string",
                    "description": "The http or https URL of the page to fetch.",
                },
            },
            required=("url",),
        ),
        side_effect=SideEffect.NETWORK,
        default_risk=RiskLevel.MEDIUM,
        allowed_profiles=ALL_PROFILES,
        handler=_fetch,
    )

    return [web_search, web_fetch]


def default_web_tools() -> list[ToolSpec]:
    """Production web tools backed by DuckDuckGoProvider and PageFetcher."""
    from shellpilot.web.fetch import PageFetcher as _PageFetcher
    from shellpilot.web.search import DuckDuckGoProvider as _DuckDuckGoProvider

    return make_web_tools(_DuckDuckGoProvider(), _PageFetcher())
