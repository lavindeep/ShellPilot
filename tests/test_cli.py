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


def test_default_invocation_routes_to_interactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Path] = []

    def fake_interactive(
        workspace: Path,
        resume: str | None = None,
        model_override: str | None = None,
        *,
        legacy_ui: bool = False,
    ) -> int:
        seen.append(workspace)
        return 0

    monkeypatch.setattr("shellpilot.cli.terminal.run_interactive", fake_interactive)
    assert run_cli(["--cwd", str(tmp_path)]) == 0
    assert seen == [tmp_path.resolve()]


def test_missing_cwd_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist"
    assert run_cli(["--cwd", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_resume_flag_parses_and_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str | None] = []

    def fake_interactive(
        workspace: Path,
        resume: str | None = None,
        model_override: str | None = None,
        *,
        legacy_ui: bool = False,
    ) -> int:
        seen.append(resume)
        return 0

    monkeypatch.setattr("shellpilot.cli.terminal.run_interactive", fake_interactive)
    assert run_cli(["--cwd", str(tmp_path), "--resume"]) == 0
    assert run_cli(["--cwd", str(tmp_path), "--resume", "20260611-101010-abcd"]) == 0
    assert seen == ["latest", "20260611-101010-abcd"]


def test_legacy_ui_flag_parses_and_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[bool] = []

    def fake_interactive(
        workspace: Path,
        resume: str | None = None,
        model_override: str | None = None,
        *,
        legacy_ui: bool = False,
    ) -> int:
        seen.append(legacy_ui)
        return 0

    monkeypatch.setattr("shellpilot.cli.terminal.run_interactive", fake_interactive)
    assert run_cli(["--cwd", str(tmp_path), "--legacy-ui"]) == 0
    assert run_cli(["--cwd", str(tmp_path)]) == 0
    assert seen == [True, False]


def test_model_flag_parsed() -> None:
    args = build_parser().parse_args(["--model", "gemma4:e2b"])
    assert args.model == "gemma4:e2b"


def test_model_flag_default_is_none() -> None:
    args = build_parser().parse_args([])
    assert args.model is None


def test_model_flag_passed_to_run_interactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str | None] = []

    def fake_interactive(
        workspace: Path,
        resume: str | None = None,
        model_override: str | None = None,
        *,
        legacy_ui: bool = False,
    ) -> int:
        seen.append(model_override)
        return 0

    monkeypatch.setattr("shellpilot.cli.terminal.run_interactive", fake_interactive)
    assert run_cli(["--cwd", str(tmp_path), "--model", "gemma4:e2b"]) == 0
    assert run_cli(["--cwd", str(tmp_path)]) == 0
    assert seen == ["gemma4:e2b", None]
