"""Tests for layered configuration loading (design section 17)."""

from pathlib import Path

import pytest

from shellpilot.config.loader import ConfigError, load_config
from shellpilot.config.model import Settings, is_tested_model


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


# ---------------------------------------------------------------------------
# is_tested_model / TESTED_FAMILIES
# ---------------------------------------------------------------------------


def test_is_tested_model_gemma4() -> None:
    assert is_tested_model("gemma4:e4b") is True


def test_is_tested_model_qwen35() -> None:
    assert is_tested_model("qwen3.5:9b-mlx") is True


def test_is_tested_model_unknown_family_returns_false() -> None:
    assert is_tested_model("llama4:x") is False


# ---------------------------------------------------------------------------
# A9: keep_alive config
# ---------------------------------------------------------------------------


def test_keep_alive_default_is_5m(tmp_path: Path) -> None:
    """keep_alive defaults to '5m' when not configured."""
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    assert loaded.settings.model.keep_alive == "5m"


def test_keep_alive_toml_override(tmp_path: Path) -> None:
    """keep_alive can be overridden via TOML."""
    user = write_toml(tmp_path / "user.toml", '[model]\nkeep_alive = "30m"\n')
    loaded = load_config(
        user_config_file=user, project_config_file=tmp_path / "missing.toml", env={}
    )
    assert loaded.settings.model.keep_alive == "30m"
    assert loaded.sources["model.keep_alive"] == "user"


# ---------------------------------------------------------------------------
# B5: [tools] config section
# ---------------------------------------------------------------------------


def test_tools_web_defaults_off(tmp_path: Path) -> None:
    """tools.web is False by default (no config files)."""
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    assert loaded.settings.tools.web is False
    assert loaded.sources["tools.web"] == "default"


def test_tools_web_toml_override(tmp_path: Path) -> None:
    """tools.web = true in a project or user toml flips it on and records the source."""
    project = write_toml(tmp_path / "proj.toml", "[tools]\nweb = true\n")
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=project,
        env={},
    )
    assert loaded.settings.tools.web is True
    assert loaded.sources["tools.web"] == "project"


def test_tools_web_rejects_non_boolean(tmp_path: Path) -> None:
    """tools.web must be a boolean; a string value is a config error."""
    user = write_toml(tmp_path / "user.toml", '[tools]\nweb = "yes"\n')
    with pytest.raises(ConfigError, match="tools.web"):
        load_config(
            user_config_file=user,
            project_config_file=tmp_path / "missing.toml",
            env={},
        )


# ---------------------------------------------------------------------------
# model.options: verbatim Ollama options passthrough (config-file only)
# ---------------------------------------------------------------------------


def test_model_options_default_empty(tmp_path: Path) -> None:
    """model.options defaults to an empty table sourced from defaults."""
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    assert loaded.settings.model.options == {}
    assert loaded.sources["model.options"] == "default"


def test_model_options_toml_round_trip(tmp_path: Path) -> None:
    """A [model.options] table is parsed verbatim and records the user source."""
    user = write_toml(
        tmp_path / "user.toml",
        "[model.options]\nrepeat_penalty = 1.3\nrepeat_last_n = 256\nseed = 7\n",
    )
    loaded = load_config(
        user_config_file=user,
        project_config_file=tmp_path / "missing.toml",
        env={},
    )
    assert loaded.settings.model.options == {
        "repeat_penalty": 1.3,
        "repeat_last_n": 256,
        "seed": 7,
    }
    assert loaded.sources["model.options"] == "user"


def test_model_options_project_replaces_user(tmp_path: Path) -> None:
    """The project layer replaces the user layer's options table wholesale."""
    user = write_toml(tmp_path / "user.toml", "[model.options]\nrepeat_penalty = 1.3\n")
    project = write_toml(tmp_path / "proj.toml", "[model.options]\ntemperature = 0.2\n")
    loaded = load_config(
        user_config_file=user,
        project_config_file=project,
        env={},
    )
    assert loaded.settings.model.options == {"temperature": 0.2}
    assert loaded.sources["model.options"] == "project"


def test_model_options_rejects_scalar(tmp_path: Path) -> None:
    """A scalar model.options is a config error naming the dotted key."""
    user = write_toml(tmp_path / "user.toml", '[model]\noptions = "hot"\n')
    with pytest.raises(ConfigError, match="model.options"):
        load_config(
            user_config_file=user,
            project_config_file=tmp_path / "missing.toml",
            env={},
        )
