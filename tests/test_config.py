"""Tests for layered configuration loading (design section 17)."""

from pathlib import Path

import pytest

from shellpilot.config.loader import ConfigError, load_config
from shellpilot.config.model import Settings


def write_toml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_defaults_when_no_files_exist(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    settings = loaded.settings
    assert isinstance(settings, Settings)
    assert settings.model.default == "gemma4:e4b"
    assert settings.runtime.security_profile == "balanced"
    assert settings.context.model_context_tokens is None  # "auto"
    assert settings.context.max_command_capture_chars == 200_000
    assert loaded.sources["model.default"] == "default"


def test_user_file_overrides_defaults(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", '[model]\ndefault = "gemma4:e2b"\n')
    loaded = load_config(
        user_config_file=user, project_config_file=tmp_path / "missing.toml", env={}
    )
    assert loaded.settings.model.default == "gemma4:e2b"
    assert loaded.sources["model.default"] == "user"


def test_project_overrides_user(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", '[runtime]\nsecurity_profile = "supervised"\n')
    project = write_toml(tmp_path / "proj.toml", '[runtime]\nsecurity_profile = "balanced"\n')
    loaded = load_config(user_config_file=user, project_config_file=project, env={})
    assert loaded.settings.runtime.security_profile == "balanced"
    assert loaded.sources["runtime.security_profile"] == "project"


def test_env_overrides_project(tmp_path: Path) -> None:
    project = write_toml(tmp_path / "proj.toml", '[model]\ndefault = "gemma4:e2b"\n')
    loaded = load_config(
        user_config_file=tmp_path / "missing.toml",
        project_config_file=project,
        env={"SHELLPILOT_MODEL": "gemma4:e4b", "SHELLPILOT_PROFILE": "supervised"},
    )
    assert loaded.settings.model.default == "gemma4:e4b"
    assert loaded.sources["model.default"] == "env"
    assert loaded.settings.runtime.security_profile == "supervised"


def test_cli_overrides_everything(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing.toml",
        project_config_file=tmp_path / "missing.toml",
        env={"SHELLPILOT_MODEL": "gemma4:e2b"},
        cli_overrides={"model.default": "gemma4:e4b"},
    )
    assert loaded.settings.model.default == "gemma4:e4b"
    assert loaded.sources["model.default"] == "cli"


def test_auto_string_means_none(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", '[context]\nmodel_context_tokens = "auto"\n')
    loaded = load_config(
        user_config_file=user, project_config_file=tmp_path / "missing.toml", env={}
    )
    assert loaded.settings.context.model_context_tokens is None


def test_explicit_int_for_auto_field(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", "[context]\nmodel_context_tokens = 16384\n")
    loaded = load_config(
        user_config_file=user, project_config_file=tmp_path / "missing.toml", env={}
    )
    assert loaded.settings.context.model_context_tokens == 16384


def test_wrong_type_reports_dotted_key(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", "[runtime]\nmax_tool_turns = true\n")
    with pytest.raises(ConfigError, match="runtime.max_tool_turns"):
        load_config(user_config_file=user, project_config_file=tmp_path / "missing.toml", env={})


def test_unknown_key_reports_dotted_key(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", "[runtime]\nmax_steps = 5\n")
    with pytest.raises(ConfigError, match="runtime.max_steps"):
        load_config(user_config_file=user, project_config_file=tmp_path / "missing.toml", env={})


def test_invalid_profile_rejected(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", '[runtime]\nsecurity_profile = "trusted-local"\n')
    with pytest.raises(ConfigError, match="trusted-local"):
        load_config(user_config_file=user, project_config_file=tmp_path / "missing.toml", env={})


def test_invalid_toml_reports_file(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", "not toml ][")
    with pytest.raises(ConfigError, match="user.toml"):
        load_config(user_config_file=user, project_config_file=tmp_path / "missing.toml", env={})


def test_ui_glyphs_and_spinner_defaults(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    assert loaded.settings.ui.glyphs == "auto"
    assert loaded.settings.ui.spinner is True


def test_ui_glyphs_validated(tmp_path: Path) -> None:
    user = write_toml(tmp_path / "user.toml", '[ui]\nglyphs = "fancy"\n')
    with pytest.raises(ConfigError, match="ui.glyphs"):
        load_config(user_config_file=user, project_config_file=tmp_path / "missing.toml", env={})


def test_ui_glyphs_env_override(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={"SHELLPILOT_UI_GLYPHS": "ascii"},
    )
    assert loaded.settings.ui.glyphs == "ascii"
    assert loaded.sources["ui.glyphs"] == "env"
