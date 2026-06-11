"""Tests for OllamaClient.chat streaming (httpx.MockTransport, no live Ollama)."""

import json
from typing import Any

import httpx

from shellpilot.llm.messages import ToolDefinition, user
from shellpilot.llm.ollama import OllamaClient


def stream_body(*chunks: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(chunk) for chunk in chunks).encode()


def test_chat_streams_content_and_sets_num_ctx() -> None:
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "content": "Hel"}, "done": False},
                {"message": {"role": "assistant", "content": "lo"}, "done": True},
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    tokens: list[str] = []
    reply = client.chat("gemma4:e4b", [user("hi")], num_ctx=4096, on_token=tokens.append)
    assert reply.content == "Hello"
    assert tokens == ["Hel", "lo"]
    payload = seen_payloads[0]
    assert payload["options"]["num_ctx"] == 4096
    assert payload["stream"] is True
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_collects_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": {"path": "a.py"},
                                }
                            }
                        ],
                    },
                    "done": True,
                }
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    tool = ToolDefinition(
        name="read_file",
        description="Read a file",
        parameters={"path": {"type": "string"}},
        required=("path",),
    )
    reply = client.chat("gemma4:e4b", [user("read a.py")], tools=[tool], num_ctx=4096)
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].arguments == {"path": "a.py"}


def test_chat_retries_without_think_when_unsupported() -> None:
    """Existing: think error causes a retry without think for that model call."""
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append(payload)
        if payload.get("think"):
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(reasoning=True, transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hi")], num_ctx=2048)
    assert reply.content == "ok"
    assert attempts[0].get("think") is True
    assert "think" not in attempts[1]


def test_think_error_retries_without_think_and_caches_model() -> None:
    """After a think-rejection for model A, subsequent calls to model A never send think."""
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append(payload)
        if payload.get("think") and payload.get("model") == "no-think:1b":
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(reasoning=True, transport=httpx.MockTransport(handler))

    # First call to the no-think model: sends think, gets error, retries without think.
    reply = client.chat("no-think:1b", [user("hi")], num_ctx=2048)
    assert reply.content == "ok"
    assert attempts[0].get("think") is True  # first attempt had think
    assert "think" not in attempts[1]  # retry did not

    attempts.clear()

    # Second call to the SAME model: should skip think entirely (no retry needed).
    reply2 = client.chat("no-think:1b", [user("hello")], num_ctx=2048)
    assert reply2.content == "ok"
    assert len(attempts) == 1
    assert "think" not in attempts[0]


def test_think_still_sent_for_other_models_after_fallback() -> None:
    """After fallback for model A, think is still sent for model B."""
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append(payload)
        if payload.get("think") and payload.get("model") == "no-think:1b":
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(reasoning=True, transport=httpx.MockTransport(handler))

    # Trigger fallback for no-think:1b.
    client.chat("no-think:1b", [user("hi")], num_ctx=2048)
    attempts.clear()

    # A different model should still get think in its request.
    client.chat("gemma4:e4b", [user("hi")], num_ctx=2048)
    assert len(attempts) == 1
    assert attempts[0].get("think") is True


def test_model_context_length_reads_model_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"model_info": {"gemma4.context_length": 32768, "other": "x"}}
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    assert client.model_context_length("gemma4:e4b") == 32768


def test_model_context_length_none_when_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    client = OllamaClient(transport=httpx.MockTransport(handler))
    assert client.model_context_length("missing") is None
