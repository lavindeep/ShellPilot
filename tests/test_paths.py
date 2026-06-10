"""Tests for filesystem path resolution."""

from pathlib import Path

from shellpilot.persistence.paths import AppPaths, project_state_dir


def test_default_paths_are_app_specific() -> None:
    paths = AppPaths.default()
    for directory in (paths.config_dir, paths.data_dir, paths.state_dir, paths.cache_dir):
        assert "shellpilot" in str(directory).lower()


def test_user_config_file_lives_in_config_dir() -> None:
    paths = AppPaths.default()
    assert paths.user_config_file == paths.config_dir / "config.toml"


def test_project_state_dir_is_local_to_workspace(tmp_path: Path) -> None:
    assert project_state_dir(tmp_path) == tmp_path / ".shellpilot"
