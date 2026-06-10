"""Tests for the Ollama HTTP client (no live Ollama; httpx.MockTransport only)."""

import httpx
import pytest

from shellpilot.llm.ollama import (
    LocalModel,
    OllamaClient,
    OllamaUnreachableError,
)

TAGS_PAYLOAD = {
    "models": [
        {"name": "gemma4:e4b", "size": 4_500_000_000},
        {"name": "gemma4:e2b", "size": 2_500_000_000},
    ]
}


def make_client(handler: httpx.MockTransport) -> OllamaClient:
    return OllamaClient(transport=handler)


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
