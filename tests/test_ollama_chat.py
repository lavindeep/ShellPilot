"""Tests for OllamaClient.chat streaming (httpx.MockTransport, no live Ollama)."""

import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from shellpilot.llm.client import GenerationCancelled
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


def test_chat_captures_thinking_without_content() -> None:
    """A reasoning-only stream returns thinking on the reply, empty content, no tokens."""
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "thinking": "Let me ", "content": ""}},
                {
                    "message": {"role": "assistant", "thinking": "reason.", "content": ""},
                    "done": True,
                },
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hi")], num_ctx=4096, on_token=tokens.append)
    assert reply.content == ""
    assert reply.thinking == "Let me reason."
    assert tokens == []


def test_chat_merges_options_with_num_ctx() -> None:
    """Configured options pass through verbatim, alongside num_ctx."""
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat(
        "gemma4:e4b",
        [user("hi")],
        num_ctx=4096,
        options={"repeat_penalty": 1.3, "seed": 7},
    )
    assert seen_payloads[0]["options"] == {"repeat_penalty": 1.3, "seed": 7, "num_ctx": 4096}


def test_chat_options_default_is_only_num_ctx() -> None:
    """With no options (or empty) the payload options is exactly {num_ctx: N}."""
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat("gemma4:e4b", [user("hi")], num_ctx=2048)
    client.chat("gemma4:e4b", [user("hi")], num_ctx=2048, options={})
    assert seen_payloads[0]["options"] == {"num_ctx": 2048}
    assert seen_payloads[1]["options"] == {"num_ctx": 2048}


def test_chat_num_ctx_overrides_options_num_ctx() -> None:
    """A num_ctx inside options is overridden by the call's num_ctx (budget owns it)."""
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat("gemma4:e4b", [user("hi")], num_ctx=4096, options={"num_ctx": 999})
    assert seen_payloads[0]["options"]["num_ctx"] == 4096


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


# ---------------------------------------------------------------------------
# Branch-6 mid-stream cancellation (§31.15)
# ---------------------------------------------------------------------------


def test_stream_chat_cancel_stops_reading_mid_stream() -> None:
    """A set cancel event aborts the stream read; the rest is never consumed."""
    cancel = threading.Event()
    pulled = {"count": 0}
    total = 6

    def gen() -> Iterator[bytes]:
        for i in range(total):
            pulled["count"] += 1
            # The user hits Ctrl-C while the 2nd chunk streams.
            if i == 1:
                cancel.set()
            chunk = {"message": {"role": "assistant", "content": f"c{i}"}, "done": i == total - 1}
            yield (json.dumps(chunk) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gen())

    client = OllamaClient(transport=httpx.MockTransport(handler))
    with pytest.raises(GenerationCancelled):
        client.chat("gemma4:e4b", [user("hi")], num_ctx=4096, cancel=cancel)
    # It raised at the read boundary after the cancelled chunk — it did NOT drain
    # the whole stream (no `done` chunk was reached).
    assert pulled["count"] < total


def test_stream_chat_cancel_not_set_completes_normally() -> None:
    """An unset (or absent) cancel event leaves streaming unaffected."""
    cancel = threading.Event()  # never set

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "content": "Hel"}, "done": False},
                {"message": {"role": "assistant", "content": "lo"}, "done": True},
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hi")], num_ctx=4096, cancel=cancel)
    assert reply.content == "Hello"


def test_chat_threads_cancel_through_think_retry() -> None:
    """A cancel during the retried (no-think) call still aborts it.

    The think attempt's 400 (a reader terminal error) is surfaced FIRST so the
    retry happens; the cancel is then raised on the retry's own stream — never
    masking the think-unsupported recovery.
    """
    cancel = threading.Event()
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append(payload)
        if payload.get("think"):
            # Pre-flight rejection → OllamaResponseError → triggers the retry.
            return httpx.Response(400, json={"error": "model does not support thinking"})

        # The retry streams; cancel is set as its first chunk is produced, so the
        # drain aborts THIS (no-think) call.
        def gen() -> Iterator[bytes]:
            cancel.set()
            chunk = {"message": {"role": "assistant", "content": "ok"}, "done": True}
            yield (json.dumps(chunk) + "\n").encode()

        return httpx.Response(200, content=gen())

    client = OllamaClient(reasoning=True, transport=httpx.MockTransport(handler))
    with pytest.raises(GenerationCancelled):
        client.chat("gemma4:e4b", [user("hi")], num_ctx=2048, cancel=cancel)
    # The retry happened (think → no-think) AND it observed the cancel.
    assert attempts[0].get("think") is True
    assert "think" not in attempts[1]


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


# ---------------------------------------------------------------------------
# on_thinking callback and eval_count → output_tokens (thinking-stream plumbing)
# ---------------------------------------------------------------------------


def test_on_thinking_fires_callback() -> None:
    """Thinking fragments forwarded to on_thinking in order; on_token still fires for content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "thinking": "Hmm ", "content": ""}},
                {"message": {"role": "assistant", "thinking": "let me think.", "content": ""}},
                {
                    "message": {"role": "assistant", "thinking": "", "content": "Answer"},
                    "done": False,
                },
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ),
        )

    thinking_chunks: list[str] = []
    content_tokens: list[str] = []
    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat(
        "gemma4:e4b",
        [user("hi")],
        num_ctx=4096,
        on_token=content_tokens.append,
        on_thinking=thinking_chunks.append,
    )
    assert thinking_chunks == ["Hmm ", "let me think."]
    assert content_tokens == ["Answer"]
    assert reply.thinking == "Hmm let me think."
    assert reply.content == "Answer"


def test_on_thinking_none_does_not_raise() -> None:
    """on_thinking=None (the default) leaves the existing stream behavior unchanged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {
                    "message": {"role": "assistant", "thinking": "thinking...", "content": ""},
                    "done": True,
                },
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hi")], num_ctx=4096)
    assert reply.thinking == "thinking..."


def test_eval_count_sets_output_tokens() -> None:
    """A done chunk carrying eval_count sets Message.output_tokens to that integer."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "content": "hi"}, "done": True, "eval_count": 42},
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hello")], num_ctx=4096)
    assert reply.output_tokens == 42


def test_eval_count_absent_gives_zero() -> None:
    """A done chunk without eval_count yields output_tokens == 0."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "content": "hi"}, "done": True},
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hello")], num_ctx=4096)
    assert reply.output_tokens == 0


def test_eval_count_non_int_gives_zero() -> None:
    """A non-int eval_count in the done chunk yields output_tokens == 0 (no raise)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body(
                {
                    "message": {"role": "assistant", "content": "hi"},
                    "done": True,
                    "eval_count": "not-an-int",
                },
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    reply = client.chat("gemma4:e4b", [user("hello")], num_ctx=4096)
    assert reply.output_tokens == 0
