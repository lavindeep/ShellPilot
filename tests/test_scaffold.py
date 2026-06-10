"""Scaffold smoke tests: the package is importable and the entrypoint runs."""

import subprocess
import sys

import pytest

from shellpilot import __version__


def test_version_is_set() -> None:
    assert __version__


def test_main_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    from shellpilot.app import main

    assert main([]) == 0
    assert "ShellPilot" in capsys.readouterr().out


def test_module_execution() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "shellpilot"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "ShellPilot" in result.stdout
