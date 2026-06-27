"""Tests for the Ollama HTTP client (no live Ollama; httpx.MockTransport only)."""

import json
from typing import Any

import httpx
import pytest

from shellpilot.llm.messages import Message
from shellpilot.llm.ollama import (
    DEFAULT_BASE_URL,
    LocalModel,
    OllamaClient,
    OllamaResponseError,
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


def test_list_models_raises_response_error_on_invalid_json() -> None:
    """A 200 with a non-JSON body raises the typed error, not a raw JSONDecodeError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<<not json>>"))
    client = make_client(transport)
    with pytest.raises(OllamaResponseError):
        client.list_models()


def test_list_models_raises_response_error_on_wrong_shape() -> None:
    """A malformed schema (models is a string, not a list) raises the typed error."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": "bad"}))
    client = make_client(transport)
    with pytest.raises(OllamaResponseError):
        client.list_models()


def test_model_capabilities_empty_on_malformed_show_json() -> None:
    """Metadata probing never crashes a session: malformed /api/show JSON -> ()."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
    client = make_client(transport)
    assert client.model_capabilities("gemma4:e4b") == ()


def test_model_context_length_none_on_non_dict_show_json() -> None:
    """A non-dict /api/show body yields None rather than an AttributeError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=["not", "a", "dict"]))
    client = make_client(transport)
    assert client.model_context_length("gemma4:e4b") is None


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


def test_client_ignores_ambient_proxy_env() -> None:
    """The httpx client must not honour ambient proxy env vars.

    Loopback Ollama traffic cannot be redirected by HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
    in the environment — trust_env=False enforces this invariant (F7 / §36.10).
    """
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert client._client.trust_env is False


# ---------------------------------------------------------------------------
# Fix #4: truncated stream (no done sentinel) is rejected
# ---------------------------------------------------------------------------


def _streaming_transport(chunks: list[dict[str, Any]]) -> httpx.MockTransport:
    """MockTransport that responds to /api/chat with the given NDJSON lines."""
    body = "\n".join(json.dumps(c) for c in chunks) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(200, content=body.encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _raw_stream_transport(body: str) -> httpx.MockTransport:
    """MockTransport that returns a verbatim /api/chat NDJSON body (any shape)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(200, content=body.encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_truncated_stream_raises_response_error() -> None:
    """A stream that closes without a done:true chunk raises OllamaResponseError."""
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": "hello"}, "done": False},
        {"message": {"role": "assistant", "content": " world"}, "done": False},
        # NOTE: no {"done": true} chunk — simulates a truncated/OOM-killed stream
    ]
    client = make_client(_streaming_transport(chunks))
    with pytest.raises(OllamaResponseError, match="done"):
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)


def test_complete_stream_returns_assembled_message() -> None:
    """A stream ending with done:true is accepted and returns the full message."""
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": "hello"}, "done": False},
        {"message": {"role": "assistant", "content": " world"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
    ]
    client = make_client(_streaming_transport(chunks))
    msg = client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)
    assert msg.content == "hello world"
    assert msg.role == "assistant"


def test_done_reason_length_stream_is_accepted() -> None:
    """A stream truncated by context length (done_reason='length') still carries done:true."""
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": "partial answer"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "length"},
    ]
    client = make_client(_streaming_transport(chunks))
    msg = client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)
    assert msg.content == "partial answer"


# ---------------------------------------------------------------------------
# Malformed stream-chunk shapes raise the typed error, not a raw AttributeError
# ---------------------------------------------------------------------------


def test_stream_non_dict_chunk_raises_response_error() -> None:
    """A top-level JSON array chunk raises OllamaResponseError, not AttributeError."""
    client = make_client(_raw_stream_transport('["not", "a", "dict"]\n'))
    with pytest.raises(OllamaResponseError, match="shape"):
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)


def test_stream_non_dict_message_raises_response_error() -> None:
    """A chunk whose `message` is a string raises OllamaResponseError."""
    body = json.dumps({"message": "oops", "done": True}) + "\n"
    client = make_client(_raw_stream_transport(body))
    with pytest.raises(OllamaResponseError, match="shape"):
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)


def test_stream_non_list_tool_calls_raises_response_error() -> None:
    """A chunk whose `message.tool_calls` is a string raises OllamaResponseError."""
    body = json.dumps({"message": {"content": "", "tool_calls": "oops"}, "done": True}) + "\n"
    client = make_client(_raw_stream_transport(body))
    with pytest.raises(OllamaResponseError, match="shape"):
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)


def test_stream_non_dict_tool_call_element_is_skipped() -> None:
    """A non-dict element in a valid tool_calls list is skipped, not raised."""
    body = (
        json.dumps(
            {
                "message": {
                    "content": "hi",
                    "tool_calls": ["oops", {"function": {"name": "x", "arguments": {}}}],
                },
                "done": True,
            }
        )
        + "\n"
    )
    client = make_client(_raw_stream_transport(body))
    msg = client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)
    assert msg.content == "hi"
    assert [c.name for c in msg.tool_calls] == ["x"]
