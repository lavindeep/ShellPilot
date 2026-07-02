from __future__ import annotations

from pathlib import Path

from shellpilot.cli.path_completion import path_completion_matches


def _labels(matches: object) -> list[str]:
    return [match.label for match in matches]


def test_cwd_set_path_suggestions_are_workspace_relative(tmp_path: Path) -> None:
    (tmp_path / "Projects").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    matches = path_completion_matches("/cwd set Pro", tmp_path)

    assert _labels(matches) == ["Projects/"]
    assert matches[0].fill == "/cwd set Projects/"


def test_path_suggestions_support_tilde_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    matches = path_completion_matches("/cwd set ~/De", tmp_path)

    assert _labels(matches) == ["~/Desktop/"]
    assert matches[0].fill == "/cwd set ~/Desktop/"


def test_non_path_slash_command_has_no_path_suggestions(tmp_path: Path) -> None:
    assert path_completion_matches("/model use ge", tmp_path) == []


def test_attach_and_export_use_path_suggestions(tmp_path: Path) -> None:
    (tmp_path / "cat.png").write_text("x", encoding="utf-8")
    (tmp_path / "exports").mkdir()

    attach = path_completion_matches("/attach ca", tmp_path)
    export = path_completion_matches("/export ex", tmp_path)

    assert _labels(attach) == ["cat.png"]
    assert attach[0].fill == "/attach cat.png"
    assert _labels(export) == ["exports/"]
    assert export[0].fill == "/export exports/"


def test_absolute_path_suggestions_keep_absolute_fill(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "alpha").mkdir()

    matches = path_completion_matches(f"/cwd set {root}/al", tmp_path)

    assert _labels(matches) == [f"{root}/alpha/"]
    assert matches[0].fill == f"/cwd set {root}/alpha/"


def test_trailing_slash_lists_inside_directory(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "shellpilot").mkdir()

    matches = path_completion_matches("/cwd set src/", tmp_path)

    assert _labels(matches) == ["src/shellpilot/"]
    assert matches[0].fill == "/cwd set src/shellpilot/"


def test_hidden_paths_only_match_when_dot_is_typed(tmp_path: Path) -> None:
    (tmp_path / ".secret").mkdir()
    (tmp_path / "src").mkdir()

    visible = path_completion_matches("/cwd set s", tmp_path)
    hidden = path_completion_matches("/cwd set .s", tmp_path)

    assert _labels(visible) == ["src/"]
    assert _labels(hidden) == [".secret/"]


def test_path_completion_escapes_spaces_in_fill(tmp_path: Path) -> None:
    (tmp_path / "My Project").mkdir()

    matches = path_completion_matches("/cwd set My", tmp_path)

    assert _labels(matches) == ["My Project/"]
    assert matches[0].fill == "/cwd set My\\ Project/"
