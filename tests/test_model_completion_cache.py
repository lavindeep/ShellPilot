from __future__ import annotations

from shellpilot.cli.model_completion_cache import ModelCompletionCache
from shellpilot.llm.ollama import LocalModel


class _Client:
    def __init__(self, models: list[LocalModel]) -> None:
        self.models = models

    def list_models(self) -> list[LocalModel]:
        return list(self.models)


class _BrokenClient:
    def list_models(self) -> list[LocalModel]:
        raise RuntimeError("ollama down")


def test_model_completion_cache_returns_snapshot_copy() -> None:
    original = [LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000)]
    cache = ModelCompletionCache(original)

    snapshot = cache.snapshot()
    snapshot.clear()

    assert [model.name for model in cache.snapshot()] == ["gemma4:e4b"]


def test_model_completion_cache_refreshes_from_client() -> None:
    cache = ModelCompletionCache([LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000)])
    client = _Client([LocalModel(name="llama3:8b", size_bytes=4_000_000_000)])

    cache.refresh_from(client)

    assert [model.name for model in cache.snapshot()] == ["llama3:8b"]


def test_model_completion_cache_keeps_old_snapshot_on_refresh_failure() -> None:
    cache = ModelCompletionCache([LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000)])

    cache.refresh_from(_BrokenClient())

    assert [model.name for model in cache.snapshot()] == ["gemma4:e4b"]
