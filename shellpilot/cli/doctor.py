"""Environment checks for `shellpilot doctor`."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shellpilot.llm.ollama import OllamaClient, OllamaError
from shellpilot.persistence.paths import AppPaths

MIN_PYTHON = (3, 11)
GEMMA_FAMILY_PREFIX = "gemma4"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one doctor check."""

    name: str
    ok: bool
    detail: str


def check_python(version: tuple[int, int, int] | None = None) -> CheckResult:
    actual = version or sys.version_info[:3]
    version_text = ".".join(str(part) for part in actual)
    if actual >= MIN_PYTHON:
        return CheckResult("Python", True, f"{version_text}")
    return CheckResult("Python", False, f"{version_text} found; 3.11+ required")


def check_ollama_binary(which: Callable[[str], str | None] = shutil.which) -> CheckResult:
    location = which("ollama")
    if location:
        return CheckResult("Ollama binary", True, location)
    return CheckResult("Ollama binary", False, "not on PATH; install from https://ollama.com")


def check_ollama_api(client: OllamaClient) -> CheckResult:
    if client.health():
        return CheckResult("Ollama API", True, "reachable")
    return CheckResult("Ollama API", False, "unreachable; is `ollama serve` running?")


def check_models(client: OllamaClient) -> CheckResult:
    try:
        models = client.list_models()
    except OllamaError:
        return CheckResult("Gemma 4 models", False, "skipped: Ollama API unreachable")
    gemma = [model.name for model in models if model.name.startswith(GEMMA_FAMILY_PREFIX)]
    if gemma:
        return CheckResult("Gemma 4 models", True, ", ".join(sorted(gemma)))
    return CheckResult(
        "Gemma 4 models", False, f"none installed; try `ollama pull {GEMMA_FAMILY_PREFIX}:e4b`"
    )


def check_dir_writable(label: str, path: Path) -> CheckResult:
    name = f"{label} dir"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".shellpilot-write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckResult(name, False, f"{path}: {exc.strerror or exc}")
    return CheckResult(name, True, str(path))


def run_all(workspace: Path, paths: AppPaths, client: OllamaClient) -> list[CheckResult]:
    return [
        check_python(),
        check_ollama_binary(),
        check_ollama_api(client),
        check_models(client),
        check_dir_writable("config", paths.config_dir),
        check_dir_writable("state", paths.state_dir),
        check_dir_writable("workspace", workspace),
    ]


def render(results: list[CheckResult], console: Console) -> None:
    table = Table(title="shellpilot doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for result in results:
        status = "[green]ok[/green]" if result.ok else "[red]fail[/red]"
        table.add_row(result.name, status, result.detail)
    console.print(table)


def run_doctor(
    workspace: Path,
    paths: AppPaths | None = None,
    client: OllamaClient | None = None,
) -> int:
    owns_client = client is None
    resolved_client = client or OllamaClient(timeout_seconds=3.0)
    try:
        results = run_all(workspace, paths or AppPaths.default(), resolved_client)
    finally:
        if owns_client:
            resolved_client.close()
    render(results, Console())
    return 0 if all(result.ok for result in results) else 1
