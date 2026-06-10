"""HTTP client for the local Ollama API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 10.0
BASE_URL_ENV_VAR = "SHELLPILOT_OLLAMA_BASE_URL"


class OllamaError(Exception):
    """Base error for Ollama client failures."""


class OllamaUnreachableError(OllamaError):
    """The Ollama API could not be reached."""


class OllamaResponseError(OllamaError):
    """The Ollama API returned an unexpected response."""


@dataclass(frozen=True)
class LocalModel:
    """A model installed in the local Ollama instance."""

    name: str
    size_bytes: int


def resolve_base_url() -> str:
    return os.environ.get(BASE_URL_ENV_VAR, DEFAULT_BASE_URL)


class OllamaClient:
    """Thin, testable wrapper over the Ollama HTTP API."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url or resolve_base_url(),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        """True when the Ollama API answers the tags endpoint."""
        try:
            response = self._client.get("/api/tags")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def list_models(self) -> list[LocalModel]:
        """All models installed locally."""
        try:
            response = self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.TransportError as exc:
            raise OllamaUnreachableError(f"Ollama API unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaResponseError(f"Ollama API error: {exc}") from exc
        payload: dict[str, Any] = response.json()
        models = payload.get("models") or []
        return [
            LocalModel(name=str(item.get("name", "")), size_bytes=int(item.get("size", 0)))
            for item in models
        ]
