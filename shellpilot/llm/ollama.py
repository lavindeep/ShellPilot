"""HTTP client for the local Ollama API."""

from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from shellpilot.llm.client import GenerationCancelled
from shellpilot.llm.messages import Message, ToolCall, ToolDefinition

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_GENERATE_TIMEOUT_SECONDS = 300.0


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
    # The Ollama endpoint is a config-file-only setting (model.base_url); it is
    # deliberately NOT read from an ambient env var, so a malicious environment
    # cannot redirect where prompts are sent (audit F7). Clients constructed
    # without an explicit base_url (e.g. doctor) fall back to the local default.
    return DEFAULT_BASE_URL


def is_loopback_url(base_url: str) -> bool:
    """True when *base_url* points at this box (loopback = local, no egress).

    The single source of truth for endpoint locality, shared by the runtime
    egress chokepoint and the CLI boot consent gate so both agree on what
    counts as off-box (design section 15.2). An empty/unset base_url falls back
    to the local default and is local; ``localhost`` / ``*.localhost`` /
    ``0.0.0.0`` and any loopback IP (127.0.0.0/8, ::1) or the unspecified
    address are local. Anything else — a different literal host, OR a non-empty
    but unparseable URL/host — is treated as remote (fail closed).
    """
    if not base_url.strip():
        return True  # unset → OllamaClient falls back to the local default
    try:
        host = (urlsplit(base_url).hostname or "").rstrip(".")
    except ValueError:
        return False  # unparseable URL → remote (fail closed)
    if not host:
        return False  # non-empty URL with no parseable host → remote (fail closed)
    if host == "localhost" or host.endswith(".localhost") or host == "0.0.0.0":  # noqa: S104
        return True
    ip_str = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


class OllamaClient:
    """Thin, testable wrapper over the Ollama HTTP API."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        generate_timeout_seconds: float = DEFAULT_GENERATE_TIMEOUT_SECONDS,
        reasoning: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url or resolve_base_url(),
            timeout=httpx.Timeout(timeout_seconds, read=generate_timeout_seconds),
            transport=transport,
            trust_env=False,
        )
        self._reasoning = reasoning
        # Per-model fallback cache: models that rejected the think flag.
        self._no_think: set[str] = set()

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
        try:
            payload = response.json()
            models = payload.get("models") or []
            return [
                LocalModel(name=str(item.get("name", "")), size_bytes=int(item.get("size", 0)))
                for item in models
            ]
        except (ValueError, TypeError, AttributeError) as exc:
            raise OllamaResponseError(f"Ollama API returned a malformed model list: {exc}") from exc

    def _api_show(self, model: str) -> dict[str, Any] | None:
        """POST /api/show; return the parsed body, or None on any HTTP/transport error."""
        try:
            response = self._client.post("/api/show", json={"model": model})
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        try:
            result = response.json()
        except ValueError:
            return None  # malformed JSON body — metadata probing must never crash
        return result if isinstance(result, dict) else None

    def model_context_length(self, model: str) -> int | None:
        """Maximum context length from model metadata, or None when undetectable.

        Note the Ollama context trap (design section 10.5): this is the model's
        maximum, NOT the runtime context. chat() must always set num_ctx.
        """
        payload = self._api_show(model)
        if payload is None:
            return None
        info = payload.get("model_info") or {}
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
        return None

    def model_capabilities(self, model: str) -> tuple[str, ...]:
        """Capabilities advertised by the model, e.g. ("completion", "vision").

        Returns an empty tuple on any HTTP or transport error so that capability
        probing never crashes a session.
        """
        payload = self._api_show(model)
        if payload is None:
            return ()
        return tuple(payload.get("capabilities") or ())

    def preload(self, model: str, *, keep_alive: str = "5m") -> None:
        """Warm the model into memory before the first user turn.

        Sends a non-streaming POST /api/chat with an empty messages list.  Ollama
        loads the model into GPU/CPU memory and returns once it is ready.  Uses the
        long read timeout (DEFAULT_GENERATE_TIMEOUT_SECONDS) that is already
        configured on the client, so even large models that take tens of seconds
        on an 8 GB machine will not time out.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [],
            "stream": False,
            "keep_alive": keep_alive,
        }
        try:
            response = self._client.post("/api/chat", json=payload)
            response.raise_for_status()
        except httpx.TransportError as exc:
            raise OllamaUnreachableError(f"Ollama API unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaResponseError(f"Ollama API error: {exc}") from exc

    def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
        num_ctx: int,
        options: dict[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Message:
        """Stream one chat completion; num_ctx is set explicitly on every request.

        Configured `options` pass through verbatim, but num_ctx ALWAYS wins:
        the context budget owns it (design section 10.5).

        ``cancel`` is a branch-6 turn-abort signal (§31.15): when the app SETS it
        (from the Ctrl-C keybinding on the loop thread), the next stream-read
        boundary raises ``GenerationCancelled``. It is threaded into the
        think-retry path too, so a cancel requested during the first (think)
        attempt still aborts the retried call.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_encode_message(message) for message in messages],
            "stream": True,
            "options": {**(options or {}), "num_ctx": num_ctx},
        }
        if tools:
            payload["tools"] = [encode_tool(tool) for tool in tools]
        if self._reasoning and model not in self._no_think:
            payload["think"] = True
        try:
            return self._stream_chat(payload, on_token, on_thinking, cancel)
        except OllamaResponseError as exc:
            # Reasoning mode unavailable for this model: cache and retry once without
            # think (design section 24.6). Other models are not affected.
            if self._reasoning and model not in self._no_think and "think" in str(exc).lower():
                self._no_think.add(model)
                payload.pop("think", None)
                return self._stream_chat(payload, on_token, on_thinking, cancel)
            raise

    def _stream_chat(
        self,
        payload: dict[str, Any],
        on_token: Callable[[str], None] | None,
        on_thinking: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Message:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        done_seen = False
        output_tokens = 0
        try:
            with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")
                    raise OllamaResponseError(f"Ollama API error {response.status_code}: {body}")
                for line in response.iter_lines():
                    # Branch-6 cancel check at the read boundary (§31.15). Raising
                    # INSIDE the `with self._client.stream(...)` block closes the
                    # response IN-THREAD via the context manager — the safe close.
                    # The app only ever SETS the event (cross-thread, thread-safe);
                    # response.close() is never called cross-thread. GenerationCancelled
                    # is neither httpx.TransportError nor OllamaResponseError, so it
                    # propagates cleanly out through chat() to the runtime.
                    if cancel is not None and cancel.is_set():
                        raise GenerationCancelled
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OllamaResponseError(f"invalid stream chunk: {line[:200]}") from exc
                    if not isinstance(chunk, dict):
                        raise OllamaResponseError(f"unexpected stream chunk shape: {line[:200]}")
                    if chunk.get("error"):
                        raise OllamaResponseError(str(chunk["error"]))
                    if chunk.get("done"):
                        done_seen = True
                        raw_count = chunk.get("eval_count")
                        output_tokens = raw_count if isinstance(raw_count, int) else 0
                    message = chunk.get("message") or {}
                    if not isinstance(message, dict):
                        raise OllamaResponseError(f"unexpected stream message shape: {line[:200]}")
                    token = message.get("content") or ""
                    if token:
                        content_parts.append(token)
                        if on_token is not None:
                            on_token(token)
                    # Reasoning text streams in a separate field; capture it so a
                    # reasoning-only turn is observable instead of silently empty
                    # (design section 24.6). May be streamed to the UI via on_thinking
                    # when a consumer is wired; never echoed back to the API.
                    thinking = message.get("thinking") or ""
                    if thinking:
                        thinking_parts.append(thinking)
                        if on_thinking is not None:
                            on_thinking(thinking)
                    raw_calls = message.get("tool_calls") or []
                    if not isinstance(raw_calls, list):
                        raise OllamaResponseError(f"unexpected tool_calls shape: {line[:200]}")
                    for raw_call in raw_calls:
                        parsed = _decode_tool_call(raw_call)
                        if parsed is not None:
                            tool_calls.append(parsed)
            if not done_seen:
                raise OllamaResponseError("stream ended before completion (no done sentinel)")
        except httpx.TransportError as exc:
            raise OllamaUnreachableError(f"Ollama API unreachable: {exc}") from exc
        return Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tuple(tool_calls),
            thinking="".join(thinking_parts),
            output_tokens=output_tokens,
        )


def _encode_message(message: Message) -> dict[str, Any]:
    encoded: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        encoded["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.images:
        encoded["images"] = [ref.data_b64 for ref in message.images]
    return encoded


def encode_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.parameters,
                "required": list(tool.required),
            },
        },
    }


def _decode_tool_call(raw: object) -> ToolCall | None:
    # A non-dict element inside an otherwise-valid tool_calls list is one bad
    # call, not a malformed stream — skip it (None) the same way a dict call
    # with a missing name/arguments is skipped, rather than raising.
    if not isinstance(raw, dict):
        return None
    function = raw.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    return ToolCall(name=name, arguments=arguments)
