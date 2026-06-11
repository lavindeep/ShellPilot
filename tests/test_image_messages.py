"""Tests for ImageRef, Message.images, Ollama encoding, and model capabilities (B7)."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx

from shellpilot.llm.messages import ImageRef, user
from shellpilot.llm.ollama import OllamaClient
from tests.conftest import TINY_PNG


def _make_image_ref(data: bytes, path: str = "/tmp/test.png") -> ImageRef:
    sha256 = hashlib.sha256(data).hexdigest()
    data_b64 = base64.b64encode(data).decode()
    return ImageRef(path=path, sha256=sha256, data_b64=data_b64)


def stream_body(*chunks: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(chunk) for chunk in chunks).encode()


# ---------------------------------------------------------------------------
# Message / ImageRef data-model tests
# ---------------------------------------------------------------------------


def test_message_images_default_empty() -> None:
    """Message.images defaults to an empty tuple when not supplied."""
    msg = user("hello")
    assert msg.images == ()


def test_user_helper_carries_images() -> None:
    """user() keyword param images= is stored on the resulting Message."""
    ref = _make_image_ref(TINY_PNG)
    msg = user("look at this", images=(ref,))
    assert len(msg.images) == 1
    assert msg.images[0] is ref
    assert msg.images[0].path == "/tmp/test.png"
    assert len(msg.images[0].sha256) == 64  # hex SHA-256
    assert msg.images[0].data_b64 == base64.b64encode(TINY_PNG).decode()


def test_image_ref_is_frozen() -> None:
    """ImageRef is an immutable frozen dataclass."""
    ref = _make_image_ref(TINY_PNG)
    try:
        ref.path = "/other"  # type: ignore[misc]
        raise AssertionError("should have raised FrozenInstanceError")
    except Exception as exc:
        assert "frozen" in str(exc).lower() or "cannot assign" in str(exc).lower()


# ---------------------------------------------------------------------------
# Ollama _encode_message tests
# ---------------------------------------------------------------------------


def test_encode_message_includes_base64_images() -> None:
    """When a Message carries ImageRefs the encoded dict has an 'images' list."""
    ref = _make_image_ref(TINY_PNG)
    msg = user("describe this", images=(ref,))

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat("gemma4:e4b", [msg], num_ctx=2048)

    assert len(seen) == 1
    encoded_msg = seen[0]["messages"][0]
    assert "images" in encoded_msg
    assert encoded_msg["images"] == [ref.data_b64]


def test_encode_message_omits_images_key_when_none() -> None:
    """When a Message has no images the 'images' key must be absent (not [])."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body({"message": {"role": "assistant", "content": "ok"}, "done": True}),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat("gemma4:e4b", [user("hi")], num_ctx=2048)

    encoded_msg = seen[0]["messages"][0]
    assert "images" not in encoded_msg


def test_chat_payload_carries_user_images() -> None:
    """Full chat payload contains the images list inside the user message."""
    ref1 = _make_image_ref(TINY_PNG, path="/home/user/img1.png")
    ref2 = _make_image_ref(TINY_PNG, path="/home/user/img2.png")
    msg = user("compare these", images=(ref1, ref2))

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"role": "assistant", "content": "done"}, "done": True}
            ),
        )

    client = OllamaClient(transport=httpx.MockTransport(handler))
    client.chat("gemma4:e4b", [msg], num_ctx=4096)

    payload = captured[0]
    user_enc = payload["messages"][0]
    assert user_enc["role"] == "user"
    assert user_enc["images"] == [ref1.data_b64, ref2.data_b64]


# ---------------------------------------------------------------------------
# model_capabilities tests
# ---------------------------------------------------------------------------


def test_model_capabilities_parsed() -> None:
    """/api/show returning capabilities list is parsed into a tuple of strings."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        body = json.loads(request.content)
        assert body["model"] == "gemma4:e4b"
        return httpx.Response(200, json={"capabilities": ["completion", "vision"]})

    client = OllamaClient(transport=httpx.MockTransport(handler))
    caps = client.model_capabilities("gemma4:e4b")
    assert caps == ("completion", "vision")


def test_model_capabilities_error_returns_empty() -> None:
    """HTTP 500 from /api/show returns an empty tuple without raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal error"})

    client = OllamaClient(transport=httpx.MockTransport(handler))
    caps = client.model_capabilities("gemma4:e4b")
    assert caps == ()


def test_model_capabilities_unreachable_returns_empty() -> None:
    """Transport error from /api/show returns an empty tuple without raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = OllamaClient(transport=httpx.MockTransport(handler))
    caps = client.model_capabilities("gemma4:e4b")
    assert caps == ()


# ---------------------------------------------------------------------------
# FakeLLM capabilities
# ---------------------------------------------------------------------------


def test_fake_llm_model_capabilities() -> None:
    """FakeLLM.model_capabilities() returns its capabilities attribute."""
    from tests.fakes.fake_llm import FakeLLM

    fake = FakeLLM()
    caps = fake.model_capabilities("gemma4:e4b")
    assert "vision" in caps
    assert "completion" in caps


def test_fake_llm_capabilities_customizable() -> None:
    """FakeLLM.capabilities can be overridden for tests that need specific caps."""
    from tests.fakes.fake_llm import FakeLLM

    fake = FakeLLM(capabilities=("completion",))
    assert fake.model_capabilities("any") == ("completion",)
