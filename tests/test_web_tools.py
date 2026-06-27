"""Tests for web_search and web_fetch tools (B6).

All tests use injected mock providers/fetchers — no real network calls.
"""

from __future__ import annotations

from pathlib import Path

from shellpilot.config.model import Settings, ToolSettings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.tools.base import ToolContext
from shellpilot.tools.web import make_web_tools
from shellpilot.web.errors import WebFetchError, WebSearchError
from shellpilot.web.fetch import FetchedPage
from shellpilot.web.search import SearchResult
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI

# ---------------------------------------------------------------------------
# Helpers: injected mock providers / fetchers
# ---------------------------------------------------------------------------


class _MockSearchProvider:
    """In-memory search provider for tests."""

    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        self.calls.append((query, max_results))
        return self._results[:max_results]


class _FailingSearchProvider:
    """Provider that always raises WebSearchError."""

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        raise WebSearchError("DDG request failed: timeout")


class _MockPageFetcher:
    """In-memory page fetcher for tests."""

    def __init__(self, page: FetchedPage) -> None:
        self._page = page
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        return self._page


class _FailingPageFetcher:
    """Fetcher that always raises WebFetchError."""

    def fetch(self, url: str) -> FetchedPage:
        raise WebFetchError(f"HTTP 404 fetching {url}")


def _make_tools_settings(web: bool = True) -> Settings:
    return Settings(tools=ToolSettings(web=web))


def _make_runtime(
    fake: FakeLLM,
    ui: FakeUI,
    tmp_path: Path,
    *,
    settings: Settings | None = None,
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )


def _empty_fetcher() -> _MockPageFetcher:
    return _MockPageFetcher(FetchedPage(url="", title="", text="", truncated=False))


# ---------------------------------------------------------------------------
# web_search: description is provider-neutral with web_fetch bridge
# ---------------------------------------------------------------------------


def test_web_search_description_is_provider_neutral() -> None:
    """web_search description must not name DuckDuckGo and must bridge to web_fetch."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")
    description = search_spec.definition.description

    assert "DuckDuckGo" not in description, (
        f"Description must not name DuckDuckGo; got: {description!r}"
    )
    assert "web_fetch" in description, (
        f"Description must reference web_fetch as the follow-up tool; got: {description!r}"
    )
    assert "leads" in description, (
        f"Description must frame results as leads, not evidence; got: {description!r}"
    )


# ---------------------------------------------------------------------------
# web_search: result formatting
# ---------------------------------------------------------------------------


def test_web_search_formats_results(tmp_path: Path) -> None:
    """Handler returns numbered list: title, url, snippet."""
    results = [
        SearchResult(title="Alpha", url="https://alpha.example.com", snippet="First result."),
        SearchResult(title="Beta", url="https://beta.example.com", snippet="Second result."),
    ]
    provider = _MockSearchProvider(results)
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = search_spec.handler(ctx, {"query": "test query"})

    assert result.success is True
    assert "1." in result.content
    assert "Alpha" in result.content
    assert "https://alpha.example.com" in result.content
    assert "First result." in result.content
    assert "2." in result.content
    assert "Beta" in result.content
    assert "2 results" in result.summary
    assert "test query" in result.summary


def test_web_search_failure_returns_failed_result(tmp_path: Path) -> None:
    """WebSearchError → success=False ToolResult, never uncaught."""
    specs = make_web_tools(_FailingSearchProvider(), _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = search_spec.handler(ctx, {"query": "failing query"})

    assert result.success is False
    assert "timeout" in result.content.lower() or "failed" in result.content.lower()


def test_web_search_empty_results_message(tmp_path: Path) -> None:
    """Empty result list → success=True, 'No results.' content."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = search_spec.handler(ctx, {"query": "nothing"})

    assert result.success is True
    assert "No results" in result.content


def test_web_search_empty_query_returns_failed_result(tmp_path: Path) -> None:
    """Empty query string → failed ToolResult without calling provider."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = search_spec.handler(ctx, {"query": ""})

    assert result.success is False
    assert provider.calls == []  # provider never called


def test_web_search_non_numeric_max_results_falls_back_to_default(tmp_path: Path) -> None:
    """A non-numeric max_results value must not raise; the provider is called with 5."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    search_spec.handler(ctx, {"query": "test", "max_results": "abc"})

    assert len(provider.calls) == 1
    _, received_max = provider.calls[0]
    assert received_max == 5


def test_web_search_excessive_max_results_is_clamped(tmp_path: Path) -> None:
    """max_results values above 10 must be clamped to 10."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    search_spec.handler(ctx, {"query": "test", "max_results": 999})

    assert len(provider.calls) == 1
    _, received_max = provider.calls[0]
    assert received_max == 10


def test_web_search_zero_max_results_is_clamped_to_one(tmp_path: Path) -> None:
    """max_results of 0 or negative must be clamped to 1."""
    provider = _MockSearchProvider([])
    specs = make_web_tools(provider, _empty_fetcher())
    search_spec = next(s for s in specs if s.name == "web_search")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    search_spec.handler(ctx, {"query": "test", "max_results": -3})

    assert len(provider.calls) == 1
    _, received_max = provider.calls[0]
    assert received_max == 1


# ---------------------------------------------------------------------------
# web_fetch: result formatting
# ---------------------------------------------------------------------------


def test_web_fetch_returns_page_text(tmp_path: Path) -> None:
    """Handler formats: 'Title: ...\nURL: ...\n\n<text>'."""
    page = FetchedPage(
        url="https://final.example.com/page",
        title="Example Page",
        text="Some body text here.",
        truncated=False,
    )
    fetcher = _MockPageFetcher(page)
    specs = make_web_tools(_MockSearchProvider([]), fetcher)
    fetch_spec = next(s for s in specs if s.name == "web_fetch")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = fetch_spec.handler(ctx, {"url": "https://example.com/page"})

    assert result.success is True
    assert "Title: Example Page" in result.content
    assert "https://final.example.com/page" in result.content
    assert "Some body text here." in result.content


def test_web_fetch_truncation_flagged(tmp_path: Path) -> None:
    """Truncated pages set result.truncated = True and include actionable marker."""
    page = FetchedPage(
        url="https://example.com/long",
        title="Long Page",
        text="x" * 100,
        truncated=True,
    )
    specs = make_web_tools(_MockSearchProvider([]), _MockPageFetcher(page))
    fetch_spec = next(s for s in specs if s.name == "web_fetch")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = fetch_spec.handler(ctx, {"url": "https://example.com/long"})

    assert result.success is True
    assert result.truncated is True
    assert "fetch a more specific official URL" in result.content


def test_web_fetch_failure_returns_failed_result(tmp_path: Path) -> None:
    """WebFetchError → success=False ToolResult, never uncaught."""
    specs = make_web_tools(_MockSearchProvider([]), _FailingPageFetcher())
    fetch_spec = next(s for s in specs if s.name == "web_fetch")

    ctx = ToolContext(workspace=tmp_path, max_result_tokens=2000)
    result = fetch_spec.handler(ctx, {"url": "https://example.com/missing"})

    assert result.success is False
    assert "404" in result.content


# ---------------------------------------------------------------------------
# Policy metadata
# ---------------------------------------------------------------------------


def test_web_tool_policy_metadata() -> None:
    """Both specs: NETWORK side effect, MEDIUM risk, both profiles allowed."""
    specs = make_web_tools(_MockSearchProvider([]), _empty_fetcher())
    assert len(specs) == 2
    for spec in specs:
        assert spec.side_effect is SideEffect.NETWORK
        assert spec.default_risk is RiskLevel.MEDIUM
        assert "supervised" in spec.allowed_profiles
        assert "balanced" in spec.allowed_profiles


# ---------------------------------------------------------------------------
# Runtime registration
# ---------------------------------------------------------------------------


def test_web_tools_absent_when_config_off(tmp_path: Path) -> None:
    """Default settings (tools.web=False) → web_search/web_fetch not in registry."""
    fake = FakeLLM(script=[])
    runtime = _make_runtime(fake, FakeUI(), tmp_path, settings=Settings())
    names = {spec.name for spec in runtime.registry.specs()}
    assert "web_search" not in names
    assert "web_fetch" not in names


def test_web_tools_registered_when_enabled(tmp_path: Path) -> None:
    """settings.tools.web=True → web_search and web_fetch registered."""
    fake = FakeLLM(script=[])
    settings = _make_tools_settings(web=True)
    runtime = _make_runtime(fake, FakeUI(), tmp_path, settings=settings)
    names = {spec.name for spec in runtime.registry.specs()}
    assert "web_search" in names
    assert "web_fetch" in names


# ---------------------------------------------------------------------------
# Approval visibility integration test
# ---------------------------------------------------------------------------


def test_web_search_approval_carries_query(tmp_path: Path) -> None:
    """Approval request display includes the query string.

    The executor's _display_for renders args as name(key=value, ...).  For
    web_search(query='test query') the display becomes
    "web_search(query='test query')" — the query is visible to the user in the
    approval prompt without any custom preview function.
    """
    provider = _MockSearchProvider(
        [SearchResult(title="T", url="https://t.example.com", snippet="s")]
    )

    settings = _make_tools_settings(web=True)
    fake = FakeLLM(
        script=[
            tool_call("web_search", query="test query"),
            answer("Found something."),
        ]
    )
    ui = FakeUI()
    ui.approve_actions = True

    runtime = ConversationRuntime(
        llm=fake,
        settings=settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )
    # Replace the default_web_tools specs with mock-backed specs (no real network)
    for spec in make_web_tools(provider, _empty_fetcher()):
        runtime.registry._specs.pop(spec.name, None)  # type: ignore[attr-defined]
        runtime.registry.register(spec)

    runtime.run_turn("search for test query")

    # At least one approval request should carry the query in its display
    displays = [r.display for r in ui.approval_requests]
    assert any("test query" in d for d in displays), (
        f"Expected 'test query' in approval display. Got: {displays}"
    )
    # And the tool result should be in history (search returned a result)
    tool_results = [m for m in runtime._history if m.role == "tool"]  # type: ignore[attr-defined]
    assert tool_results, "Expected at least one tool result in history"
