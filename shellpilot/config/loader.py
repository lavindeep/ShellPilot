"""Layered configuration loading with per-key source tracking (design section 17).

Precedence, highest wins: CLI overrides > environment > project config > user
config > built-in defaults. SHELLPILOT_CONFIG (alternate user config path) is
resolved by the CLI before calling load_config.
"""

from __future__ import annotations

import dataclasses
import difflib
import tomllib
import types
import typing
from dataclasses import dataclass
from dataclasses import field as dc_field
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
    SkillSettings,
    ToolSettings,
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
    "tools": ToolSettings,
    "skills": SkillSettings,
}

ENV_MAP: dict[str, str] = {
    "SHELLPILOT_MODEL": "model.default",
    "SHELLPILOT_NO_COLOR": "ui.no_color",
    "SHELLPILOT_UI_GLYPHS": "ui.glyphs",
    # Egress and security-posture keys (tools.web, model.base_url,
    # model.allow_cloud, runtime.security_profile) are deliberately absent:
    # enabling network egress or cloud models, redirecting the Ollama endpoint,
    # or downgrading the security profile must be a DELIBERATE act (a config.toml
    # edit or a confirm-gated /config set), never an ambient environment
    # variable. This env-absence invariant is load-bearing. See HIGH_STAKES_KEYS.
}

ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "runtime.security_profile": VALID_PROFILES,
    "privacy.allow_sensitive_reads": ("ask", "never", "always"),
    "workspace.boundary": ("start_cwd",),
    "model.provider": ("ollama",),
    "ui.glyphs": ("auto", "unicode", "ascii"),
}

MIN_VALUES: dict[str, int] = {
    "runtime.command_timeout_seconds": 1,
    "runtime.max_tool_turns": 1,
    "runtime.max_plan_steps": 1,
}

# Exclusive float bounds: 0 < ratio < 1 for context ratios.
RANGE_VALUES: dict[str, tuple[float, float]] = {
    "context.compact_at_ratio": (0.0, 1.0),
    "context.hard_limit_ratio": (0.0, 1.0),
}

# Keys consumed only at boot (console/theme construction, model client,
# keep_alive preload, store construction, tool registration).  Everything else
# takes effect next turn via update_settings.
BOOT_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "model.reasoning",
        "model.base_url",
        "model.keep_alive",
        "model.default",
        "instructions.load_agents_md",
        "privacy.redact_secrets",
        "ui.theme",
        "ui.no_color",
        "ui.glyphs",
        "ui.spinner",
        "tools.web",
        "model.allow_cloud",
    }
)

# Structural tables/lists with no scalar CLI representation — settable ONLY by
# editing config.toml (the user or project layer), never via an env var, the
# program-managed overrides.json, or /config set.
CONFIG_FILE_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "model.options",
        "skills.enabled",
    }
)

# Egress / safety keys whose runtime change materially affects whether data
# leaves the device or the local approval posture. They are settable via the
# DELIBERATE /config set act — but only behind an amber warning + explicit
# confirmation (the HIGH_STAKES_KEYS gate in the slash handler) — and via a
# config.toml edit. They are deliberately ABSENT from ENV_MAP: an ambient env
# var is not a deliberate act, so it must never enable egress or downgrade the
# security posture. The per-session cloud-consent gate (§15.2) is the real
# egress boundary regardless of how allow_cloud was set.
HIGH_STAKES_KEYS: frozenset[str] = frozenset(
    {
        "tools.web",
        "model.base_url",
        "runtime.security_profile",
        "model.allow_cloud",
    }
)

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class LoadedConfig:
    """Resolved settings plus the source layer of every key."""

    settings: Settings
    sources: dict[str, str]
    warnings: tuple[str, ...] = dc_field(default_factory=tuple)


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

    if key == "model.options":
        # Verbatim Ollama options table. Individual keys are NOT validated by
        # ShellPilot; Ollama validates and errors at request time. Config-file
        # only by design (no env-var mapping, no ALLOWED_VALUES) — sampling
        # changes must be an explicit config act, same rationale as tools.web.
        if not isinstance(value, dict):
            raise ConfigError(f"{key}: expected a table, got {value!r}")
        return dict(value)

    if key == "skills.enabled":
        # TOML array of skill names. Config-file only by design — no env-var,
        # no overrides, no /config set (see SkillSettings in model.py).
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ConfigError(f"{key}: expected a list of strings, got {value!r}")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            stripped = item.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                result.append(stripped)
        return tuple(result)

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
        minimum = MIN_VALUES.get(key)
        if minimum is not None and coerced < minimum:
            raise ConfigError(f"{key}: {value!r} must be >= {minimum}")
    elif annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key}: expected a number, got {value!r}")
        coerced = float(value)
        bounds = RANGE_VALUES.get(key)
        if bounds is not None:
            low, high = bounds
            if not (low < coerced < high):
                raise ConfigError(f"{key}: {value!r} must be > {low} and < {high}")
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
    overrides_file: Path | None = None,
) -> LoadedConfig:
    from shellpilot.config.overrides import load_overrides

    if overrides_file is None:
        overrides_file = user_config_file.parent / "overrides.json"

    values = _defaults_flat()
    sources = {key: "default" for key in values}
    warnings: list[str] = []

    for layer_name, path in (("user", user_config_file), ("project", project_config_file)):
        if not path.is_file():
            continue
        for key, raw in _flatten(_read_toml(path), path).items():
            values[key] = _coerce(key, raw)
            sources[key] = layer_name

    # Cross-field check: compact_at_ratio must be < hard_limit_ratio.
    # Evaluated after user+project layers so that config.toml inversions are
    # caught fatally before the overrides layer runs.
    _COMPACT_KEY = "context.compact_at_ratio"
    _HARD_KEY = "context.hard_limit_ratio"
    compact_pre: float = values[_COMPACT_KEY]
    hard_pre: float = values[_HARD_KEY]
    if compact_pre >= hard_pre:
        src_compact = sources[_COMPACT_KEY]
        src_hard = sources[_HARD_KEY]
        if src_compact in ("user", "project") or src_hard in ("user", "project"):
            raise ConfigError(
                f"{_COMPACT_KEY} ({compact_pre!r}) must be less than {_HARD_KEY} ({hard_pre!r})"
            )

    # Snapshot pre-override values/sources for the ratio keys so we can revert
    # any override that introduces an inversion.
    _ratio_keys = (_COMPACT_KEY, _HARD_KEY)
    _ratio_pre_values = {k: values[k] for k in _ratio_keys}
    _ratio_pre_sources = {k: sources[k] for k in _ratio_keys}

    # Overrides layer: after user+project, before env/CLI.  Self-healing:
    # unknown keys, invalid values, and file-level errors are collected as
    # warnings and never raise.
    raw_overrides, file_warnings = load_overrides(overrides_file)
    warnings.extend(file_warnings)
    for key, raw in raw_overrides.items():
        if key not in _SCHEMA:
            close = difflib.get_close_matches(key, _SCHEMA.keys(), n=1, cutoff=0.6)
            hint = f"; did you mean {close[0]!r}?" if close else ""
            warnings.append(f"overrides: unknown key {key!r}{hint} — entry ignored")
            continue
        if key in CONFIG_FILE_ONLY_KEYS:
            warnings.append(
                f"overrides: {key} is config-file only and cannot be set "
                f"via overrides — entry ignored"
            )
            continue
        try:
            values[key] = _coerce(key, raw)
            sources[key] = "set"
        except ConfigError as exc:
            warnings.append(f"overrides: {key}={raw!r} — {exc} — entry ignored")

    # Cross-field check post-overrides: if an override introduced an inversion,
    # self-heal by reverting the offending override key(s).
    compact_post: float = values[_COMPACT_KEY]
    hard_post: float = values[_HARD_KEY]
    if compact_post >= hard_post:
        reverted: list[str] = []
        for k in _ratio_keys:
            if sources[k] == "set":
                reverted.append(k)
                values[k] = _ratio_pre_values[k]
                sources[k] = _ratio_pre_sources[k]
        if reverted:
            warnings.append(
                f"overrides: {' and '.join(reverted)} dropped — would make"
                f" compact_at_ratio ({compact_post!r}) >= hard_limit_ratio"
                f" ({hard_post!r}); reverted to pre-override values"
            )

    for env_name, key in ENV_MAP.items():
        if env_name in env:
            values[key] = _coerce_env(key, env[env_name])
            sources[key] = "env"

    for key, raw in (cli_overrides or {}).items():
        values[key] = _coerce(key, raw)
        sources[key] = "cli"

    section_instances: dict[str, Any] = {}
    for section, cls in SECTIONS.items():
        kwargs = {f.name: values[f"{section}.{f.name}"] for f in dataclasses.fields(cls)}
        section_instances[section] = cls(**kwargs)
    return LoadedConfig(
        settings=Settings(**section_instances),
        sources=sources,
        warnings=tuple(warnings),
    )


def validate_override(key: str, value: Any) -> Any:
    """Validate and coerce a single key/value for use as a runtime override.

    String inputs are coerced exactly like env-var strings (e.g. ``"40"``→40,
    ``"true"``/``"false"``→bool, ``"auto"``→None for ``int | None`` fields,
    ``"0.8"``→float).

    Raises :class:`ConfigError` for:
    - unknown keys (with a close-match hint when one exists)
    - config-file-only keys (see :data:`CONFIG_FILE_ONLY_KEYS`)
    - values that fail type/range/enum validation
    """
    if key not in _SCHEMA:
        close = difflib.get_close_matches(key, _SCHEMA.keys(), n=1, cutoff=0.6)
        if close:
            raise ConfigError(f"unknown config key: {key!r}; did you mean {close[0]!r}?")
        raise ConfigError(f"unknown config key: {key!r}")
    if key in CONFIG_FILE_ONLY_KEYS:
        raise ConfigError(f"{key} is config-file only and cannot be set via /config set")
    # Coerce strings exactly as env-var parsing does.
    if isinstance(value, str):
        annotation = _SCHEMA[key]
        if annotation is bool:
            return _coerce_env(key, value)
        if annotation is int:
            # _coerce_env parses the string; then _coerce enforces MIN_VALUES.
            coerced_int = _coerce_env(key, value)
            return _coerce(key, coerced_int)
        if _is_optional_int(annotation):
            if value == "auto":
                return None
            try:
                coerced = int(value)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f'{key}: expected an integer or "auto", got {value!r}') from exc
            return _coerce(key, coerced)
        if annotation is float:
            try:
                coerced_f = float(value)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"{key}: expected a number, got {value!r}") from exc
            return _coerce(key, coerced_f)
        # str annotation: fall through to normal _coerce
    return _coerce(key, value)
