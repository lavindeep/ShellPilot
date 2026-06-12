"""Tests for layered configuration loading (design section 17)."""

from pathlib import Path

import pytest

from shellpilot.config.loader import ConfigError, load_config, validate_override
from shellpilot.config.model import Settings, is_tested_model
from shellpilot.config.overrides import load_overrides, overrides_path, save_overrides


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


# ---------------------------------------------------------------------------
# MIN_VALUES: non-positive runtime limits rejected at load time (v0.5.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,toml_section,field",
    [
        ("runtime.command_timeout_seconds", "runtime", "command_timeout_seconds"),
        ("runtime.max_tool_turns", "runtime", "max_tool_turns"),
        ("runtime.max_plan_steps", "runtime", "max_plan_steps"),
    ],
)
def test_zero_rejected_for_runtime_int_keys(
    tmp_path: Path, key: str, toml_section: str, field: str
) -> None:
    """0 must be rejected for each min-bounded runtime key."""
    user = write_toml(tmp_path / "user.toml", f"[{toml_section}]\n{field} = 0\n")
    with pytest.raises(ConfigError, match=key):
        load_config(
            user_config_file=user,
            project_config_file=tmp_path / "missing.toml",
            env={},
        )


@pytest.mark.parametrize(
    "key,toml_section,field",
    [
        ("runtime.command_timeout_seconds", "runtime", "command_timeout_seconds"),
        ("runtime.max_tool_turns", "runtime", "max_tool_turns"),
        ("runtime.max_plan_steps", "runtime", "max_plan_steps"),
    ],
)
def test_negative_rejected_for_runtime_int_keys(
    tmp_path: Path, key: str, toml_section: str, field: str
) -> None:
    """-5 must be rejected for each min-bounded runtime key."""
    user = write_toml(tmp_path / "user.toml", f"[{toml_section}]\n{field} = -5\n")
    with pytest.raises(ConfigError, match=key):
        load_config(
            user_config_file=user,
            project_config_file=tmp_path / "missing.toml",
            env={},
        )


@pytest.mark.parametrize(
    "key,toml_section,field",
    [
        ("runtime.command_timeout_seconds", "runtime", "command_timeout_seconds"),
        ("runtime.max_tool_turns", "runtime", "max_tool_turns"),
        ("runtime.max_plan_steps", "runtime", "max_plan_steps"),
    ],
)
def test_one_accepted_for_runtime_int_keys(
    tmp_path: Path, key: str, toml_section: str, field: str
) -> None:
    """1 must be accepted as the minimum valid value."""
    user = write_toml(tmp_path / "user.toml", f"[{toml_section}]\n{field} = 1\n")
    loaded = load_config(
        user_config_file=user,
        project_config_file=tmp_path / "missing.toml",
        env={},
    )
    section_name, attr = key.split(".", 1)
    assert getattr(getattr(loaded.settings, section_name), attr) == 1


def test_unrelated_int_key_unaffected_by_min_values(tmp_path: Path) -> None:
    """An unrelated int key (context.model_context_tokens) is not subject to MIN_VALUES."""
    user = write_toml(tmp_path / "user.toml", "[context]\nmodel_context_tokens = 4096\n")
    loaded = load_config(
        user_config_file=user,
        project_config_file=tmp_path / "missing.toml",
        env={},
    )
    assert loaded.settings.context.model_context_tokens == 4096


# ---------------------------------------------------------------------------
# Overrides layer: overrides.py helpers
# ---------------------------------------------------------------------------


def test_overrides_path_returns_sibling_of_config_dir(tmp_path: Path) -> None:
    """overrides_path returns config_dir/overrides.json."""
    assert overrides_path(tmp_path) == tmp_path / "overrides.json"


def test_load_overrides_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing overrides file returns ({}, []) — no warning."""
    values, warnings = load_overrides(tmp_path / "overrides.json")
    assert values == {}
    assert warnings == []


def test_save_load_overrides_round_trip(tmp_path: Path) -> None:
    """save_overrides then load_overrides preserves the dict."""
    path = tmp_path / "overrides.json"
    payload = {"model.default": "gemma4:e2b", "runtime.max_tool_turns": 20}
    save_overrides(path, payload)
    values, warnings = load_overrides(path)
    assert values == payload
    assert warnings == []


def test_save_overrides_empty_dict_writes_file(tmp_path: Path) -> None:
    """Saving {} writes an empty JSON object (file is not deleted)."""
    path = tmp_path / "overrides.json"
    save_overrides(path, {})
    assert path.exists()
    values, warnings = load_overrides(path)
    assert values == {}
    assert warnings == []


def test_load_overrides_corrupt_json_returns_warning(tmp_path: Path) -> None:
    """Corrupt JSON in overrides produces a file-level warning and empty dict."""
    path = tmp_path / "overrides.json"
    path.write_text("not json ][", encoding="utf-8")
    values, warnings = load_overrides(path)
    assert values == {}
    assert len(warnings) == 1
    assert str(path) in warnings[0]
    # File must NOT be deleted
    assert path.exists()


def test_load_overrides_non_dict_top_level_returns_warning(tmp_path: Path) -> None:
    """A JSON array at the top level is not a valid overrides file."""
    path = tmp_path / "overrides.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    values, warnings = load_overrides(path)
    assert values == {}
    assert len(warnings) == 1
    assert str(path) in warnings[0]


# ---------------------------------------------------------------------------
# Overrides layer: precedence in load_config
# ---------------------------------------------------------------------------


def _cfg(  # type: ignore[type-arg]
    tmp_path: Path,
    overrides: dict,
    *,
    user_toml: str = "",
    project_toml: str = "",
    env: dict | None = None,
    cli: dict | None = None,
):
    """Helper: write overrides file + optional TOML files and load config."""
    import json

    over_path = tmp_path / "overrides.json"
    over_path.write_text(json.dumps(overrides), encoding="utf-8")
    user = tmp_path / "user.toml"
    if user_toml:
        write_toml(user, user_toml)
    project = tmp_path / "proj.toml"
    if project_toml:
        write_toml(project, project_toml)
    return load_config(
        user_config_file=user,
        project_config_file=project,
        env=env or {},
        cli_overrides=cli,
        overrides_file=over_path,
    )


def test_override_beats_user_config(tmp_path: Path) -> None:
    """An override value wins over the user config layer."""
    loaded = _cfg(
        tmp_path,
        {"model.default": "gemma4:e4b"},
        user_toml='[model]\ndefault = "gemma4:e2b"\n',
    )
    assert loaded.settings.model.default == "gemma4:e4b"
    assert loaded.sources["model.default"] == "set"


def test_override_beats_project_config(tmp_path: Path) -> None:
    """An override value wins over the project config layer."""
    loaded = _cfg(
        tmp_path,
        {"runtime.security_profile": "supervised"},
        project_toml='[runtime]\nsecurity_profile = "balanced"\n',
    )
    assert loaded.settings.runtime.security_profile == "supervised"
    assert loaded.sources["runtime.security_profile"] == "set"


def test_env_beats_override(tmp_path: Path) -> None:
    """An env var still wins over the overrides layer."""
    loaded = _cfg(
        tmp_path,
        {"model.default": "gemma4:e2b"},
        env={"SHELLPILOT_MODEL": "gemma4:e4b"},
    )
    assert loaded.settings.model.default == "gemma4:e4b"
    assert loaded.sources["model.default"] == "env"


def test_cli_beats_env_and_override(tmp_path: Path) -> None:
    """CLI override is highest precedence, beating env and overrides layer."""
    loaded = _cfg(
        tmp_path,
        {"model.default": "gemma4:e2b"},
        env={"SHELLPILOT_MODEL": "gemma4:e2b"},
        cli={"model.default": "gemma4:e4b"},
    )
    assert loaded.settings.model.default == "gemma4:e4b"
    assert loaded.sources["model.default"] == "cli"


def test_override_unknown_key_skipped_with_warning(tmp_path: Path) -> None:
    """An unknown key in overrides is skipped and a warning names it."""
    loaded = _cfg(tmp_path, {"model.no_such_key": "x"})
    assert loaded.warnings
    assert any("no_such_key" in w for w in loaded.warnings)
    # Default model must still apply
    assert loaded.settings.model.default == "gemma4:e4b"


def test_override_model_options_skipped_with_warning(tmp_path: Path) -> None:
    """model.options cannot be set via overrides; entry is skipped with a warning."""
    loaded = _cfg(tmp_path, {"model.options": {"temperature": 0.9}})
    assert loaded.warnings
    assert any("model.options" in w for w in loaded.warnings)
    assert loaded.settings.model.options == {}


def test_override_enum_violation_skipped_with_warning(tmp_path: Path) -> None:
    """An invalid enum value in overrides is skipped; a warning names the key."""
    loaded = _cfg(tmp_path, {"runtime.security_profile": "badprofile"})
    assert loaded.warnings
    assert any("runtime.security_profile" in w for w in loaded.warnings)
    assert loaded.settings.runtime.security_profile == "balanced"


def test_override_min_value_violation_skipped_with_warning(tmp_path: Path) -> None:
    """A value below MIN_VALUES in overrides is skipped with a warning."""
    loaded = _cfg(tmp_path, {"runtime.max_tool_turns": 0})
    assert loaded.warnings
    assert any("runtime.max_tool_turns" in w for w in loaded.warnings)
    # Default is not 0
    assert loaded.settings.runtime.max_tool_turns >= 1


def test_override_wrong_type_skipped_with_warning(tmp_path: Path) -> None:
    """A wrong-typed value in overrides is skipped with a warning."""
    loaded = _cfg(tmp_path, {"runtime.max_tool_turns": "not-an-int"})
    assert loaded.warnings
    assert any("runtime.max_tool_turns" in w for w in loaded.warnings)


def test_corrupt_overrides_file_config_still_loads(tmp_path: Path) -> None:
    """A corrupt overrides file produces a warning but config loads from TOML/defaults."""
    user = write_toml(tmp_path / "user.toml", '[model]\ndefault = "gemma4:e2b"\n')
    over_path = tmp_path / "overrides.json"
    over_path.write_text("}{invalid", encoding="utf-8")
    loaded = load_config(
        user_config_file=user,
        project_config_file=tmp_path / "missing.toml",
        env={},
        overrides_file=over_path,
    )
    assert loaded.settings.model.default == "gemma4:e2b"
    assert loaded.sources["model.default"] == "user"
    assert loaded.warnings
    assert any(str(over_path) in w for w in loaded.warnings)


def test_warnings_empty_in_happy_path(tmp_path: Path) -> None:
    """When there are no overrides issues, warnings tuple is empty."""
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    assert loaded.warnings == ()


def test_override_sources_set_for_applied_entry(tmp_path: Path) -> None:
    """A successfully applied override has sources[key] == 'set'."""
    loaded = _cfg(tmp_path, {"runtime.max_tool_turns": 5})
    assert loaded.sources["runtime.max_tool_turns"] == "set"
    assert loaded.settings.runtime.max_tool_turns == 5


# ---------------------------------------------------------------------------
# validate_override
# ---------------------------------------------------------------------------


def test_validate_override_int_string_coercion(tmp_path: Path) -> None:
    """String "40" coerces to integer 40 for an int key."""
    assert validate_override("runtime.max_tool_turns", "40") == 40


def test_validate_override_true_string(tmp_path: Path) -> None:
    """String "true" coerces to True for a bool key."""
    assert validate_override("runtime.auto_compact", "true") is True


def test_validate_override_false_string(tmp_path: Path) -> None:
    """String "false" coerces to False for a bool key."""
    assert validate_override("runtime.auto_compact", "false") is False


def test_validate_override_auto_string_for_optional_int(tmp_path: Path) -> None:
    """String "auto" coerces to None for an int | None field."""
    assert validate_override("context.model_context_tokens", "auto") is None


def test_validate_override_float_string_coercion(tmp_path: Path) -> None:
    """String "0.8" coerces to float 0.8 for a float key."""
    result = validate_override("context.compact_at_ratio", "0.8")
    assert isinstance(result, float)
    assert abs(result - 0.8) < 1e-9


def test_validate_override_valid_enum(tmp_path: Path) -> None:
    """A valid enum value passes validation."""
    assert validate_override("runtime.security_profile", "supervised") == "supervised"


def test_validate_override_bad_enum_raises(tmp_path: Path) -> None:
    """An invalid enum value raises ConfigError."""
    with pytest.raises(ConfigError):
        validate_override("runtime.security_profile", "root")


def test_validate_override_unknown_key_raises(tmp_path: Path) -> None:
    """An unknown key raises ConfigError naming the key."""
    with pytest.raises(ConfigError, match="not_a_real_key"):
        validate_override("model.not_a_real_key", "x")


def test_validate_override_model_options_raises(tmp_path: Path) -> None:
    """model.options raises ConfigError (config-file only)."""
    with pytest.raises(ConfigError, match="model.options"):
        validate_override("model.options", {"temperature": 0.5})


def test_validate_override_min_values_enforced(tmp_path: Path) -> None:
    """ "0" for runtime.max_tool_turns raises ConfigError (MIN_VALUES)."""
    with pytest.raises(ConfigError):
        validate_override("runtime.max_tool_turns", "0")
