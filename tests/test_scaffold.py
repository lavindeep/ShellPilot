"""Scaffold smoke tests: the package is importable and the entrypoint runs."""

import subprocess
import sys

import pytest

from shellpilot import __version__


def test_version_is_set() -> None:
    assert __version__


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    from shellpilot.app import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "shellpilot" in capsys.readouterr().out


def test_module_execution() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "shellpilot", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "shellpilot" in result.stdout
