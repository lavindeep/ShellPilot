"""Layered configuration loading with per-key source tracking (design section 17).

Precedence, highest wins: CLI overrides > environment > project config > user
config > built-in defaults. SHELLPILOT_CONFIG (alternate user config path) is
resolved by the CLI before calling load_config.
"""

from __future__ import annotations

import dataclasses
import tomllib
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shellpilot.config.model import (
    VALID_PROFILES,
    ContextSettings,
    InstructionSettings,
    ModelSettings,
    PrivacySettings,
    RuntimeSettings,
    Settings,
    UiSettings,
    WorkspaceSettings,
)


class ConfigError(Exception):
    """A configuration file or override is invalid."""


SECTIONS: dict[str, type] = {
    "model": ModelSettings,
    "runtime": RuntimeSettings,
    "context": ContextSettings,
    "workspace": WorkspaceSettings,
    "instructions": InstructionSettings,
    "privacy": PrivacySettings,
    "ui": UiSettings,
}

ENV_MAP: dict[str, str] = {
    "SHELLPILOT_OLLAMA_BASE_URL": "model.base_url",
    "SHELLPILOT_MODEL": "model.default",
    "SHELLPILOT_PROFILE": "runtime.security_profile",
    "SHELLPILOT_NO_COLOR": "ui.no_color",
    "SHELLPILOT_UI_GLYPHS": "ui.glyphs",
}

ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "runtime.security_profile": VALID_PROFILES,
    "privacy.allow_sensitive_reads": ("ask", "never", "always"),
    "workspace.boundary": ("start_cwd",),
    "model.provider": ("ollama",),
    "ui.glyphs": ("auto", "unicode", "ascii"),
}

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class LoadedConfig:
    """Resolved settings plus the source layer of every key."""

    settings: Settings
    sources: dict[str, str]


def _field_types() -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for section, cls in SECTIONS.items():
        hints = typing.get_type_hints(cls)
        for field in dataclasses.fields(cls):
            schema[f"{section}.{field.name}"] = hints[field.name]
    return schema


_SCHEMA = _field_types()


def _is_optional_int(annotation: Any) -> bool:
    if isinstance(annotation, types.UnionType):
        return set(typing.get_args(annotation)) == {int, type(None)}
    return False


def _coerce(key: str, value: Any) -> Any:
    annotation = _SCHEMA.get(key)
    if annotation is None:
        raise ConfigError(f"unknown config key: {key}")

    coerced: Any
    if _is_optional_int(annotation):
        if value == "auto" or value is None:
            coerced = None
        elif isinstance(value, int) and not isinstance(value, bool):
            coerced = value
        else:
            raise ConfigError(f'{key}: expected an integer or "auto", got {value!r}')
    elif annotation is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{key}: expected a boolean, got {value!r}")
        coerced = value
    elif annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key}: expected an integer, got {value!r}")
        coerced = value
    elif annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key}: expected a number, got {value!r}")
        coerced = float(value)
    elif annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key}: expected a string, got {value!r}")
        coerced = value
    else:  # pragma: no cover - schema only uses the types above
        raise ConfigError(f"{key}: unsupported config type {annotation!r}")

    allowed = ALLOWED_VALUES.get(key)
    if allowed is not None and coerced not in allowed:
        raise ConfigError(f"{key}: {coerced!r} is not one of {', '.join(allowed)}")
    return coerced


def _coerce_env(key: str, raw: str) -> Any:
    annotation = _SCHEMA[key]
    if annotation is bool:
        lowered = raw.strip().lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        raise ConfigError(f"{key}: cannot parse boolean from {raw!r}")
    if annotation is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key}: cannot parse integer from {raw!r}") from exc
    return _coerce(key, raw)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _flatten(data: dict[str, Any], path: Path) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for section, body in data.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: top-level key {section!r} must be a table")
        if section not in SECTIONS:
            raise ConfigError(f"{path}: unknown config section: {section}")
        for name, value in body.items():
            flat[f"{section}.{name}"] = value
    return flat


def _defaults_flat() -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for section, cls in SECTIONS.items():
        instance = cls()
        for field in dataclasses.fields(cls):
            flat[f"{section}.{field.name}"] = getattr(instance, field.name)
    return flat


def load_config(
    *,
    user_config_file: Path,
    project_config_file: Path,
    env: dict[str, str],
    cli_overrides: dict[str, Any] | None = None,
) -> LoadedConfig:
    values = _defaults_flat()
    sources = {key: "default" for key in values}

    for layer_name, path in (("user", user_config_file), ("project", project_config_file)):
        if not path.is_file():
            continue
        for key, raw in _flatten(_read_toml(path), path).items():
            values[key] = _coerce(key, raw)
            sources[key] = layer_name

    for env_name, key in ENV_MAP.items():
        if env_name in env:
            values[key] = _coerce_env(key, env[env_name])
            sources[key] = "env"

    for key, raw in (cli_overrides or {}).items():
        values[key] = _coerce(key, raw)
        sources[key] = "cli"

    section_instances: dict[str, Any] = {}
    for section, cls in SECTIONS.items():
        kwargs = {
            field.name: values[f"{section}.{field.name}"] for field in dataclasses.fields(cls)
        }
        section_instances[section] = cls(**kwargs)
    return LoadedConfig(settings=Settings(**section_instances), sources=sources)
