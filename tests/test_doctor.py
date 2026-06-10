"""Tests for `shellpilot doctor` checks (no live Ollama; everything injected)."""

from pathlib import Path

import httpx

from shellpilot.cli.doctor import (
    check_dir_writable,
    check_models,
    check_ollama_api,
    check_ollama_binary,
    check_python,
    run_all,
)
from shellpilot.llm.ollama import OllamaClient
from shellpilot.persistence.paths import AppPaths

TAGS_PAYLOAD = {"models": [{"name": "gemma4:e4b", "size": 4_500_000_000}]}
NO_GEMMA_PAYLOAD = {"models": [{"name": "llama3:8b", "size": 4_000_000_000}]}


def ok_client() -> OllamaClient:
    return OllamaClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=TAGS_PAYLOAD))
    )


def down_client() -> OllamaClient:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return OllamaClient(transport=httpx.MockTransport(raise_connect))


def test_python_check_passes_on_modern_version() -> None:
    result = check_python((3, 12, 0))
    assert result.ok


def test_python_check_fails_below_311() -> None:
    result = check_python((3, 10, 9))
    assert not result.ok
    assert "3.11" in result.detail


def test_binary_check_uses_which() -> None:
    assert check_ollama_binary(lambda name: "/usr/local/bin/ollama").ok
    assert not check_ollama_binary(lambda name: None).ok


def test_api_check_reports_reachability() -> None:
    assert check_ollama_api(ok_client()).ok
    assert not check_ollama_api(down_client()).ok


def test_models_check_requires_gemma4() -> None:
    assert check_models(ok_client()).ok
    no_gemma = OllamaClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=NO_GEMMA_PAYLOAD))
    )
    result = check_models(no_gemma)
    assert not result.ok
    assert "ollama pull" in result.detail


def test_models_check_fails_cleanly_when_api_down() -> None:
    result = check_models(down_client())
    assert not result.ok


def test_dir_writable(tmp_path: Path) -> None:
    assert check_dir_writable("state", tmp_path / "new" / "nested").ok


def test_dir_not_writable_when_parent_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert not check_dir_writable("state", blocker / "child").ok


def test_run_all_aggregates_all_checks(tmp_path: Path) -> None:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
    )
    results = run_all(workspace=tmp_path, paths=paths, client=ok_client())
    assert len(results) >= 6
    names = [result.name for result in results]
    assert names == sorted(set(names), key=names.index)  # unique, stable order
