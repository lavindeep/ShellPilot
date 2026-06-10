"""Tests for command-line parsing and dispatch."""

from pathlib import Path

import pytest

from shellpilot.cli.commands import build_parser, run_cli


def test_doctor_subcommand_parses() -> None:
    args = build_parser().parse_args(["doctor"])
    assert args.command == "doctor"


def test_cwd_flag_parses() -> None:
    args = build_parser().parse_args(["--cwd", "/tmp"])
    assert args.cwd == Path("/tmp")


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["--version"])
    assert excinfo.value.code == 0
    assert "shellpilot" in capsys.readouterr().out


def test_default_invocation_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli([]) == 0
    assert "ShellPilot" in capsys.readouterr().out


def test_missing_cwd_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist"
    assert run_cli(["--cwd", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err
