"""Tests for the Ollama HTTP client (no live Ollama; httpx.MockTransport only)."""

import json
import queue
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from shellpilot.llm.client import GenerationCancelled
from shellpilot.llm.messages import Message
from shellpilot.llm.ollama import (
    DEFAULT_BASE_URL,
    LocalModel,
    OllamaClient,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaUnreachableError,
    describe_turn_error,
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


# ---------------------------------------------------------------------------
# Friendly, leak-free turn-error reporting (a cloud 502's raw body carries
# internal IPs / infra JSON — it must never reach the user channel).
# ---------------------------------------------------------------------------

# The exact shape seen live: a cloud gateway 502 whose body embeds tcp endpoints.
LEAKY_502_BODY = (
    '{"error":"Post \\"https://ollama.com:443/api/chat?ts=1782762226\\": '
    'read tcp 10.160.82.198:62219->34.36.133.15:443: read: operation timed out"}'
)


def _status_transport(status: int, body: str = "") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(status, content=body.encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_gateway_error_carries_status_and_drops_raw_body() -> None:
    """A 4xx/5xx response raises with the status code attached, but the raw
    upstream body (internal IPs / infra JSON) is NOT in the exception message."""
    client = make_client(_status_transport(502, LEAKY_502_BODY))
    with pytest.raises(OllamaResponseError) as exc_info:
        client.chat("gemma4:31b-cloud", [Message(role="user", content="hi")], num_ctx=2048)
    err = exc_info.value
    assert err.status_code == 502
    text = str(err)
    for leak in ("10.160.82.198", "34.36.133.15", "operation timed out", "tcp"):
        assert leak not in text


def test_stream_error_chunk_does_not_leak_raw_body() -> None:
    """A gateway failure delivered as a STREAM CHUNK (not a 400 status) is
    sanitized like the status path: the raw body (IPs/JSON) never reaches str(exc)
    or describe_turn_error, but is kept in `detail` for the think-unsupported check."""
    leak = "read tcp 10.160.82.198:62219->34.36.133.15:443: operation timed out"
    body = json.dumps({"error": leak}) + "\n"
    client = make_client(_raw_stream_transport(body))
    with pytest.raises(OllamaResponseError) as exc_info:
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)
    err = exc_info.value
    for shown in (str(err), describe_turn_error(err)):
        assert "10.160.82.198" not in shown
        assert "34.36.133.15" not in shown
        assert "operation timed out" not in shown
    assert leak in err.detail  # retained internally only


def test_describe_turn_error_gateway_is_friendly_and_leak_free() -> None:
    msg = describe_turn_error(OllamaResponseError("Ollama API error 502", status_code=502))
    assert "HTTP 502" in msg
    assert "timed out" in msg.lower() or "unavailable" in msg.lower()
    for leak in ("10.160.82.198", "34.36.133.15", "{", "tcp"):
        assert leak not in msg


def test_describe_turn_error_unreachable_suggests_serve() -> None:
    msg = describe_turn_error(OllamaUnreachableError("Ollama API unreachable: boom"))
    assert "ollama serve" in msg
    assert "boom" not in msg


def test_describe_turn_error_other_status_shows_code() -> None:
    msg = describe_turn_error(OllamaResponseError("Ollama API error 404", status_code=404))
    assert "HTTP 404" in msg


def test_describe_turn_error_malformed_stream_is_generic() -> None:
    msg = describe_turn_error(OllamaResponseError("stream ended before completion"))
    assert "malformed" in msg.lower() or "incomplete" in msg.lower()


def test_describe_turn_error_generic_exception_is_safe() -> None:
    msg = describe_turn_error(RuntimeError("secret-internal-detail 10.0.0.1"))
    assert "10.0.0.1" not in msg
    assert "RuntimeError" in msg


def test_describe_turn_error_timeout_is_friendly() -> None:
    msg = describe_turn_error(OllamaTimeoutError("the model stopped responding"))
    assert "stopped responding" in msg.lower()


# ---------------------------------------------------------------------------
# Responsive cancellation + inactivity cap (the stream-drain logic, tested
# directly with a controlled queue + injected clock — no real waits, no network).
# ---------------------------------------------------------------------------


def _fake_clock(values: list[float]) -> Callable[[], float]:
    """A clock returning successive values, holding the last once exhausted."""
    it = iter(values)
    held = [values[0]]

    def clock() -> float:
        try:
            held[0] = next(it)
        except StopIteration:
            pass
        return held[0]

    return clock


def test_drain_stream_cancel_aborts_a_hung_read_without_waiting() -> None:
    """A set cancel aborts IMMEDIATELY even when no data is arriving (a hung cloud
    read): the worker polls the queue and observes cancel, never blocked on the
    read. This is the core fix — Ctrl-C during a stall used to do nothing."""
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    lines: queue.Queue[object] = queue.Queue()  # empty: nothing ever arrives (a stall)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(GenerationCancelled):
        client._drain_stream(lines, {}, cancel, None, None)


def test_drain_stream_inactivity_cap_raises_timeout() -> None:
    """A silent stall past the inactivity cap fails with a clean OllamaTimeoutError
    rather than hanging — driven by an injected clock, so no real waiting."""
    client = OllamaClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        generate_timeout_seconds=300.0,
        monotonic=_fake_clock([0.0, 0.0, 9999.0]),  # jumps past the cap on the 2nd poll
    )
    lines: queue.Queue[object] = queue.Queue()  # empty: a true stall
    with pytest.raises(OllamaTimeoutError):
        client._drain_stream(lines, {}, None, None, None)


def test_read_timeout_surfaces_as_friendly_timeout() -> None:
    """A real httpx read timeout (hung connection) surfaces as OllamaTimeoutError
    → a friendly 'stopped responding' line, not a raw transport dump."""

    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read operation timed out", request=request)

    client = make_client(httpx.MockTransport(raise_timeout))
    with pytest.raises(OllamaTimeoutError):
        client.chat("gemma4:e4b", [Message(role="user", content="hi")], num_ctx=2048)
