"""HTTP client for the local Ollama API."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from shellpilot.llm.messages import Message, ToolCall, ToolDefinition

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_GENERATE_TIMEOUT_SECONDS = 300.0
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
        generate_timeout_seconds: float = DEFAULT_GENERATE_TIMEOUT_SECONDS,
        reasoning: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url or resolve_base_url(),
            timeout=httpx.Timeout(timeout_seconds, read=generate_timeout_seconds),
            transport=transport,
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
        payload: dict[str, Any] = response.json()
        models = payload.get("models") or []
        return [
            LocalModel(name=str(item.get("name", "")), size_bytes=int(item.get("size", 0)))
            for item in models
        ]

    def model_context_length(self, model: str) -> int | None:
        """Maximum context length from model metadata, or None when undetectable.

        Note the Ollama context trap (design section 10.5): this is the model's
        maximum, NOT the runtime context. chat() must always set num_ctx.
        """
        try:
            response = self._client.post("/api/show", json={"model": model})
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        info = response.json().get("model_info") or {}
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
        return None

    def model_capabilities(self, model: str) -> tuple[str, ...]:
        """Capabilities advertised by the model, e.g. ("completion", "vision").

        Returns an empty tuple on any HTTP or transport error so that capability
        probing never crashes a session.
        """
        try:
            response = self._client.post("/api/show", json={"model": model})
            response.raise_for_status()
        except httpx.HTTPError:
            return ()
        payload: dict[str, Any] = response.json()
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
    ) -> Message:
        """Stream one chat completion; num_ctx is set explicitly on every request.

        Configured `options` pass through verbatim, but num_ctx ALWAYS wins:
        the context budget owns it (design section 10.5).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_encode_message(message) for message in messages],
            "stream": True,
            "options": {**(options or {}), "num_ctx": num_ctx},
        }
        if tools:
            payload["tools"] = [_encode_tool(tool) for tool in tools]
        if self._reasoning and model not in self._no_think:
            payload["think"] = True
        try:
            return self._stream_chat(payload, on_token)
        except OllamaResponseError as exc:
            # Reasoning mode unavailable for this model: cache and retry once without
            # think (design section 24.6). Other models are not affected.
            if self._reasoning and model not in self._no_think and "think" in str(exc).lower():
                self._no_think.add(model)
                payload.pop("think", None)
                return self._stream_chat(payload, on_token)
            raise

    def _stream_chat(
        self, payload: dict[str, Any], on_token: Callable[[str], None] | None
    ) -> Message:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        try:
            with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")
                    raise OllamaResponseError(f"Ollama API error {response.status_code}: {body}")
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OllamaResponseError(f"invalid stream chunk: {line[:200]}") from exc
                    if chunk.get("error"):
                        raise OllamaResponseError(str(chunk["error"]))
                    message = chunk.get("message") or {}
                    token = message.get("content") or ""
                    if token:
                        content_parts.append(token)
                        if on_token is not None:
                            on_token(token)
                    # Reasoning text streams in a separate field; capture it so a
                    # reasoning-only turn is observable instead of silently empty
                    # (design section 24.6). It is never streamed to the UI.
                    thinking = message.get("thinking") or ""
                    if thinking:
                        thinking_parts.append(thinking)
                    for raw_call in message.get("tool_calls") or []:
                        parsed = _decode_tool_call(raw_call)
                        if parsed is not None:
                            tool_calls.append(parsed)
        except httpx.TransportError as exc:
            raise OllamaUnreachableError(f"Ollama API unreachable: {exc}") from exc
        return Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tuple(tool_calls),
            thinking="".join(thinking_parts),
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


def _encode_tool(tool: ToolDefinition) -> dict[str, Any]:
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


def _decode_tool_call(raw: dict[str, Any]) -> ToolCall | None:
    function = raw.get("function") or {}
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
