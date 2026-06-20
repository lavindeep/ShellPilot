"""Tests for the Ollama HTTP client (no live Ollama; httpx.MockTransport only)."""

import json

import httpx
import pytest

from shellpilot.llm.ollama import (
    DEFAULT_BASE_URL,
    LocalModel,
    OllamaClient,
    OllamaUnreachableError,
    resolve_base_url,
)

TAGS_PAYLOAD = {
    "models": [
        {"name": "gemma4:e4b", "size": 4_500_000_000},
        {"name": "gemma4:e2b", "size": 2_500_000_000},
    ]
}


def make_client(handler: httpx.MockTransport) -> OllamaClient:
    return OllamaClient(transport=handler)


def test_resolve_base_url_ignores_ambient_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint is config-file-only: an ambient env var must not redirect it (audit F7)."""
    monkeypatch.setenv("SHELLPILOT_OLLAMA_BASE_URL", "http://evil.example")
    assert resolve_base_url() == DEFAULT_BASE_URL


def test_health_true_when_tags_endpoint_responds() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=TAGS_PAYLOAD))
    client = make_client(transport)
    assert client.health() is True


def test_health_false_when_unreachable() -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(httpx.MockTransport(raise_connect))
    assert client.health() is False


def test_list_models_parses_names_and_sizes() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=TAGS_PAYLOAD))
    client = make_client(transport)
    models = client.list_models()
    assert models == [
        LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000),
        LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000),
    ]


def test_list_models_raises_when_unreachable() -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(httpx.MockTransport(raise_connect))
    with pytest.raises(OllamaUnreachableError):
        client.list_models()


def test_list_models_tolerates_empty_payload() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []}))
    client = make_client(transport)
    assert client.list_models() == []


# ---------------------------------------------------------------------------
# A9: preload tests
# ---------------------------------------------------------------------------


def test_preload_posts_empty_chat_with_keep_alive() -> None:
    """preload() sends a non-streaming POST /api/chat with messages=[] and keep_alive."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": ""}})

    client = make_client(httpx.MockTransport(handler))
    client.preload("gemma4:e4b", keep_alive="10m")

    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == "/api/chat"
    body = json.loads(req.content)
    assert body["model"] == "gemma4:e4b"
    assert body["messages"] == []
    assert body["stream"] is False
    assert body["keep_alive"] == "10m"


def test_preload_uses_default_keep_alive() -> None:
    """preload() defaults to keep_alive='5m' when not specified."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": ""}})

    client = make_client(httpx.MockTransport(handler))
    client.preload("gemma4:e4b")

    body = json.loads(captured[0].content)
    assert body["keep_alive"] == "5m"


def test_preload_raises_unreachable_on_transport_error() -> None:
    """preload() raises OllamaUnreachableError when the transport fails."""

    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(httpx.MockTransport(raise_connect))
    with pytest.raises(OllamaUnreachableError):
        client.preload("gemma4:e4b")
