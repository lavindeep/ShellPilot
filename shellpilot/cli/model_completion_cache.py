"""Thread-safe model list cache for prompt completion."""

from __future__ import annotations

import threading
from typing import Protocol

from shellpilot.llm.ollama import LocalModel


class _ModelLister(Protocol):
    def list_models(self) -> list[LocalModel]: ...


class ModelCompletionCache:
    def __init__(self, models: list[LocalModel]) -> None:
        self._lock = threading.Lock()
        self._models = list(models)

    def snapshot(self) -> list[LocalModel]:
        with self._lock:
            return list(self._models)

    def refresh_from(self, client: _ModelLister) -> None:
        try:
            models = client.list_models()
        except Exception:  # noqa: BLE001 - stale completions are better than a noisy prompt
            return
        with self._lock:
            self._models = list(models)


def refresh_model_completion_cache_in_background(
    cache: ModelCompletionCache, client: _ModelLister
) -> None:
    thread = threading.Thread(
        target=lambda: cache.refresh_from(client),
        name="model-completion-refresh",
        daemon=True,
    )
    thread.start()
