"""Skill discovery and parsing (design section 23)."""

from __future__ import annotations

import importlib.resources
import importlib.resources.abc
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from shellpilot.runtime.budget import estimate_tokens, truncate_to_tokens
from shellpilot.skills.model import Skill, SkillResource, SkillScript, SkillTrigger
from shellpilot.skills.triggers import TriggerContext, any_fires

SKILL_FILENAME = "SKILL.md"
MAX_RESOURCE_BYTES = 64 * 1024
MAX_RESOURCES_PER_KIND = 16

_SCRIPT_REQUIRED_KEYS: tuple[str, ...] = (
    "name",
    "entry",
    "description",
    "mode",
    "timeout_seconds",
)
_DiscoveryResult = tuple[
    tuple[SkillResource, ...],
    tuple[SkillResource, ...],
    tuple[SkillScript, ...],
    tuple[str, ...],
]

# Builtin skill names that are always enabled (harness machinery).
_ALWAYS_ENABLED_BUILTINS: frozenset[str] = frozenset({"planning"})

_PLANNING_TRIGGERS: tuple[SkillTrigger, ...] = (
    SkillTrigger.PLAN_PROPOSED,
    SkillTrigger.PLAN_ACTIVE,
    SkillTrigger.PLAN_BLOCKED,
)
_DEFAULT_TRIGGERS: tuple[SkillTrigger, ...] = (SkillTrigger.ENABLED,)


def _triggers_for_skill_name(name: str) -> tuple[SkillTrigger, ...]:
    return _PLANNING_TRIGGERS if name == "planning" else _DEFAULT_TRIGGERS


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str, bool, str]:
    """Parse SKILL.md frontmatter.

    Returns (meta, body, valid, error).  Never raises.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "", False, "missing opening --- delimiter"

    meta: dict[str, str] = {}
    closing = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = i
            break
        if ":" not in line:
            return {}, "", False, f"malformed frontmatter line: {line!r}"
        raw_key, _, raw_value = line.partition(":")
        key = raw_key.strip()
        if key not in ("name", "description"):
            return {}, "", False, f"unknown frontmatter key: {key!r}"
        meta[key] = raw_value.strip()

    if closing == -1:
        return {}, "", False, "missing closing --- delimiter"

    body = "\n".join(lines[closing + 1 :]).strip()
    return meta, body, True, ""


def parse_skill_md(text: str, *, folder_name: str, max_tokens: int) -> Skill:
    """Parse SKILL.md text; the folder name is authoritative for the skill name.

    Always returns a Skill; sets valid=False and error on parse failures.
    """
    meta, body, valid, error = _parse_frontmatter(text)
    if not valid:
        return Skill(
            name=folder_name,
            description="",
            body="",
            root="",
            triggers=_DEFAULT_TRIGGERS,
            est_tokens=0,
            valid=False,
            error=error,
        )

    # Folder name is authoritative; frontmatter name is advisory only.
    warnings: tuple[str, ...] = ()
    if "name" in meta and meta["name"] != folder_name:
        warnings = (f"frontmatter name {meta['name']!r} ignored — folder name is authoritative",)

    description = meta.get("description", "")
    bounded_body, _ = truncate_to_tokens(body, max_tokens)
    est = estimate_tokens(bounded_body)

    return Skill(
        name=folder_name,
        description=description,
        body=bounded_body,
        root="",  # caller sets root
        triggers=_DEFAULT_TRIGGERS,  # caller sets triggers
        est_tokens=est,
        valid=True,
        error="",
        warnings=warnings,
    )


def _is_safe_path(skill_root: Path, candidate: Path) -> bool:
    """Return whether candidate resolves inside skill_root."""
    try:
        resolved_root = skill_root.resolve()
        resolved_candidate = candidate.resolve()
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _resource_from_bytes(
    *,
    kind: str,
    name: str,
    rel_path: str,
    data: bytes,
    max_tokens: int,
) -> SkillResource:
    text = data[:MAX_RESOURCE_BYTES].decode("utf-8")
    bounded, _ = truncate_to_tokens(text, max_tokens)
    return SkillResource(
        kind=kind,
        name=name,
        rel_path=rel_path,
        text=bounded,
        est_tokens=estimate_tokens(bounded),
    )


def _read_resource_bytes_path(entry: Path) -> bytes:
    with entry.open("rb") as handle:
        return handle.read(MAX_RESOURCE_BYTES)


def _read_resource_bytes_traversable(
    entry: importlib.resources.abc.Traversable,
) -> bytes:
    with entry.open("rb") as handle:
        return handle.read(MAX_RESOURCE_BYTES)


def _discover_markdown_resources_path(
    *,
    skill_root: Path,
    dirname: str,
    kind: str,
    max_tokens: int,
) -> tuple[tuple[SkillResource, ...], tuple[str, ...]]:
    resource_dir = skill_root / dirname
    if not resource_dir.is_dir():
        return (), ()

    warnings: list[str] = []
    try:
        candidates = sorted(
            (
                entry
                for entry in resource_dir.iterdir()
                if entry.is_file() and entry.suffix == ".md"
            ),
            key=lambda entry: entry.stem,
        )
    except OSError:
        return (), (f"{dirname}/: could not read resources",)
    resources: list[SkillResource] = []
    for entry in candidates[:MAX_RESOURCES_PER_KIND]:
        rel_path = f"{dirname}/{entry.name}"
        if not _is_safe_path(skill_root, entry):
            warnings.append(f"{rel_path}: skipped unsafe path")
            continue
        try:
            data = _read_resource_bytes_path(entry)
        except OSError:
            warnings.append(f"{rel_path}: could not read resource")
            continue
        try:
            resources.append(
                _resource_from_bytes(
                    kind=kind,
                    name=entry.stem,
                    rel_path=rel_path,
                    data=data,
                    max_tokens=max_tokens,
                )
            )
        except UnicodeDecodeError:
            warnings.append(f"{rel_path}: could not decode resource")
    if len(candidates) > MAX_RESOURCES_PER_KIND:
        warnings.append(
            f"{dirname}: found {len(candidates)} markdown resources; "
            f"loaded first {MAX_RESOURCES_PER_KIND} sorted by name"
        )
    return tuple(resources), tuple(warnings)


def _discover_markdown_resources_traversable(
    *,
    skill_root: importlib.resources.abc.Traversable,
    dirname: str,
    kind: str,
    max_tokens: int,
) -> tuple[tuple[SkillResource, ...], tuple[str, ...]]:
    resource_dir = skill_root / dirname
    if not resource_dir.is_dir():
        return (), ()

    warnings: list[str] = []
    try:
        candidates = sorted(
            (
                entry
                for entry in resource_dir.iterdir()
                if entry.is_file() and entry.name.endswith(".md")
            ),
            key=lambda entry: Path(entry.name).stem,
        )
    except Exception:  # noqa: BLE001 — advisory only
        return (), (f"{dirname}/: could not read resources",)
    resources: list[SkillResource] = []
    for entry in candidates[:MAX_RESOURCES_PER_KIND]:
        rel_path = f"{dirname}/{entry.name}"
        try:
            data = _read_resource_bytes_traversable(entry)
        except Exception:  # noqa: BLE001 — unreadable resource is advisory only
            warnings.append(f"{rel_path}: could not read resource")
            continue
        try:
            resources.append(
                _resource_from_bytes(
                    kind=kind,
                    name=Path(entry.name).stem,
                    rel_path=rel_path,
                    data=data,
                    max_tokens=max_tokens,
                )
            )
        except UnicodeDecodeError:
            warnings.append(f"{rel_path}: could not decode resource")
    if len(candidates) > MAX_RESOURCES_PER_KIND:
        warnings.append(
            f"{dirname}: found {len(candidates)} markdown resources; "
            f"loaded first {MAX_RESOURCES_PER_KIND} sorted by name"
        )
    return tuple(resources), tuple(warnings)


def _invalid_script(
    *,
    name: str,
    entry: str,
    description: str = "",
    mode: str = "read",
    timeout_seconds: int = 0,
    error: str,
) -> SkillScript:
    return SkillScript(
        name=name,
        entry=entry,
        description=description,
        mode=mode,
        timeout_seconds=max(0, timeout_seconds),
        valid=False,
        error=error,
    )


def _script_entry_error_path(skill_root: Path, entry: str) -> str:
    scripts_dir = skill_root / "scripts"
    script_path = scripts_dir / entry
    if not _is_safe_path(skill_root, script_path):
        return f"entry path escapes skill root: {entry}"
    if not _is_safe_path(scripts_dir, script_path):
        return f"entry path escapes scripts/: {entry}"
    if not script_path.is_file():
        return f"entry does not exist under scripts/: {entry}"
    return ""


def _script_entry_error_traversable(
    skill_root: importlib.resources.abc.Traversable,
    entry: str,
) -> str:
    script_file = skill_root / "scripts" / entry
    if not script_file.is_file():
        return f"entry does not exist under scripts/: {entry}"
    return ""


def _script_from_manifest_entry(
    raw: Any,
    *,
    entry_error: Callable[[str], str],
) -> SkillScript:
    if not isinstance(raw, dict):
        return _invalid_script(
            name="",
            entry="",
            error="manifest entry must be an object",
        )

    for key in _SCRIPT_REQUIRED_KEYS:
        if key not in raw:
            timeout = raw["timeout_seconds"] if isinstance(raw.get("timeout_seconds"), int) else 0
            return _invalid_script(
                name=str(raw.get("name", "")),
                entry=str(raw.get("entry", "")),
                description=str(raw.get("description", "")),
                mode=str(raw.get("mode", "read")),
                timeout_seconds=timeout,
                error=f"missing required key: {key}",
            )

    name = raw["name"]
    entry = raw["entry"]
    description = raw["description"]
    mode = raw["mode"]
    timeout_seconds = raw["timeout_seconds"]
    if not all(isinstance(value, str) for value in (name, entry, description, mode)):
        return _invalid_script(
            name=str(name),
            entry=str(entry),
            description=str(description),
            mode=str(mode),
            timeout_seconds=timeout_seconds if isinstance(timeout_seconds, int) else 0,
            error="name, entry, description, and mode must be strings",
        )
    if mode not in ("read", "write"):
        return _invalid_script(
            name=name,
            entry=f"scripts/{entry}",
            description=description,
            mode=mode,
            timeout_seconds=timeout_seconds if isinstance(timeout_seconds, int) else 0,
            error="mode must be 'read' or 'write'",
        )
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        return _invalid_script(
            name=name,
            entry=f"scripts/{entry}",
            description=description,
            mode=mode,
            error="timeout_seconds must be a positive int",
        )

    entry_path = Path(entry)
    if entry_path.is_absolute() or entry_path.name != entry or entry_path.name in ("", ".", ".."):
        return _invalid_script(
            name=name,
            entry=f"scripts/{entry}",
            description=description,
            mode=mode,
            timeout_seconds=timeout_seconds,
            error="entry must be a bare relative filename",
        )

    error = entry_error(entry)
    if error:
        return _invalid_script(
            name=name,
            entry=f"scripts/{entry}",
            description=description,
            mode=mode,
            timeout_seconds=timeout_seconds,
            error=error,
        )

    return SkillScript(
        name=name,
        entry=f"scripts/{entry}",
        description=description,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )


def _script_from_manifest_entry_path(skill_root: Path, raw: Any) -> SkillScript:
    return _script_from_manifest_entry(
        raw,
        entry_error=lambda entry: _script_entry_error_path(skill_root, entry),
    )


def _discover_scripts_path(skill_root: Path) -> tuple[tuple[SkillScript, ...], tuple[str, ...]]:
    scripts_dir = skill_root / "scripts"
    if not scripts_dir.is_dir():
        return (), ()

    manifest = scripts_dir / "manifest.json"
    if not manifest.is_file():
        try:
            has_direct_files = any(entry.is_file() for entry in scripts_dir.iterdir())
        except OSError:
            return (), ("scripts/: could not read scripts directory; scripts ignored",)
        if has_direct_files:
            return (), ("scripts/: contains files but no manifest.json; scripts ignored",)
        return (), ()
    if not _is_safe_path(skill_root, manifest) or not _is_safe_path(scripts_dir, manifest):
        return (
            (
                _invalid_script(
                    name="manifest",
                    entry="scripts/manifest.json",
                    error="scripts/manifest.json: skipped unsafe path",
                ),
            ),
            (),
        )

    try:
        raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            (
                _invalid_script(
                    name="manifest",
                    entry="scripts/manifest.json",
                    error=f"malformed scripts/manifest.json: {exc}",
                ),
            ),
            (),
        )

    if not isinstance(raw_manifest, list):
        return (
            (
                _invalid_script(
                    name="manifest",
                    entry="scripts/manifest.json",
                    error="scripts/manifest.json must contain a list",
                ),
            ),
            (),
        )

    scripts = tuple(_script_from_manifest_entry_path(skill_root, entry) for entry in raw_manifest)
    return scripts, ()


def _script_from_manifest_entry_traversable(
    skill_root: importlib.resources.abc.Traversable,
    raw: Any,
) -> SkillScript:
    return _script_from_manifest_entry(
        raw,
        entry_error=lambda entry: _script_entry_error_traversable(skill_root, entry),
    )


def _discover_scripts_traversable(
    skill_root: importlib.resources.abc.Traversable,
) -> tuple[tuple[SkillScript, ...], tuple[str, ...]]:
    scripts_dir = skill_root / "scripts"
    if not scripts_dir.is_dir():
        return (), ()

    manifest = scripts_dir / "manifest.json"
    if not manifest.is_file():
        try:
            has_direct_files = any(entry.is_file() for entry in scripts_dir.iterdir())
        except Exception:  # noqa: BLE001 — advisory only
            has_direct_files = False
        if has_direct_files:
            return (), ("scripts/: contains files but no manifest.json; scripts ignored",)
        return (), ()

    try:
        raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — advisory invalid script, not fatal
        return (
            (
                _invalid_script(
                    name="manifest",
                    entry="scripts/manifest.json",
                    error=f"malformed scripts/manifest.json: {exc}",
                ),
            ),
            (),
        )
    if not isinstance(raw_manifest, list):
        return (
            (
                _invalid_script(
                    name="manifest",
                    entry="scripts/manifest.json",
                    error="scripts/manifest.json must contain a list",
                ),
            ),
            (),
        )
    scripts = tuple(
        _script_from_manifest_entry_traversable(skill_root, entry) for entry in raw_manifest
    )
    return scripts, ()


def _discover_resources_path(
    skill_root: Path,
    *,
    max_tokens: int,
) -> _DiscoveryResult:
    references, ref_warnings = _discover_markdown_resources_path(
        skill_root=skill_root,
        dirname="references",
        kind="reference",
        max_tokens=max_tokens,
    )
    templates, template_warnings = _discover_markdown_resources_path(
        skill_root=skill_root,
        dirname="templates",
        kind="template",
        max_tokens=max_tokens,
    )
    scripts, script_warnings = _discover_scripts_path(skill_root)
    return references, templates, scripts, ref_warnings + template_warnings + script_warnings


def _discover_resources_traversable(
    skill_root: importlib.resources.abc.Traversable,
    *,
    max_tokens: int,
) -> _DiscoveryResult:
    references, ref_warnings = _discover_markdown_resources_traversable(
        skill_root=skill_root,
        dirname="references",
        kind="reference",
        max_tokens=max_tokens,
    )
    templates, template_warnings = _discover_markdown_resources_traversable(
        skill_root=skill_root,
        dirname="templates",
        kind="template",
        max_tokens=max_tokens,
    )
    scripts, script_warnings = _discover_scripts_traversable(skill_root)
    return references, templates, scripts, ref_warnings + template_warnings + script_warnings


def merge_skills(builtin: list[Skill], user: list[Skill]) -> list[Skill]:
    """Merge builtin and user skills, reserving builtin names.

    A user skill whose name matches any builtin name (valid or not) becomes
    invalid with error="reserved builtin name".  Builtin names are harness
    machinery; a local folder must not be able to override them.
    """
    builtin_names: set[str] = {s.name for s in builtin}
    merged_user: list[Skill] = []
    for skill in user:
        if skill.name in builtin_names:
            merged_user.append(replace(skill, valid=False, error="reserved builtin name"))
        else:
            merged_user.append(skill)
    return list(builtin) + merged_user


def is_enabled(skill: Skill, enabled: tuple[str, ...]) -> bool:
    """Return whether a skill is currently enabled.

    The builtin planning skill is always considered enabled (harness machinery).
    Other skills follow the enabled list.
    """
    plan_status = (
        "active" if skill.root == "builtin" and skill.name in _ALWAYS_ENABLED_BUILTINS else None
    )
    ctx = TriggerContext(plan_status=plan_status, web_enabled=False, enabled=enabled)
    return any_fires(skill.triggers, skill.name, ctx)


def discover_skills(
    *,
    user_skills_dir: Path,
    enabled: tuple[str, ...],
    max_tokens: int,
) -> list[Skill]:
    """Discover skills from builtin and user roots.

    Returns ALL found skills (valid + invalid) in deterministic order:
    builtin alphabetical first, then user alphabetical.  Enablement is
    data — callers use is_enabled() to filter.
    """
    builtin_skills: list[Skill] = []
    user_skills: list[Skill] = []

    # --- Builtin root ---
    try:
        builtin_root: importlib.resources.abc.Traversable = importlib.resources.files(
            "shellpilot.skills.builtin"
        )
        entries: list[importlib.resources.abc.Traversable] = sorted(
            (e for e in builtin_root.iterdir() if e.is_dir()),
            key=lambda e: e.name,
        )
        for entry in entries:
            skill_file = entry / SKILL_FILENAME
            # Skip dirs without a SKILL.md (e.g. __pycache__)
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — unreadable → invalid entry
                skill = Skill(
                    name=entry.name,
                    description="",
                    body="",
                    root="builtin",
                    triggers=_triggers_for_skill_name(entry.name),
                    est_tokens=0,
                    valid=False,
                    error="could not read SKILL.md",
                )
                builtin_skills.append(skill)
                continue
            raw = parse_skill_md(text, folder_name=entry.name, max_tokens=max_tokens)
            references, templates, scripts, resource_warnings = _discover_resources_traversable(
                entry,
                max_tokens=max_tokens,
            )
            skill = replace(
                raw,
                root="builtin",
                triggers=_triggers_for_skill_name(entry.name),
                references=references,
                templates=templates,
                scripts=scripts,
                warnings=raw.warnings + resource_warnings,
            )
            builtin_skills.append(skill)
    except Exception:  # noqa: BLE001 — importlib resolution failure is non-fatal
        pass

    # --- User root ---
    if user_skills_dir.is_dir():
        for entry_path in sorted(user_skills_dir.iterdir(), key=lambda p: p.name):
            if not entry_path.is_dir():
                continue
            skill_file = entry_path / SKILL_FILENAME
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — unreadable → invalid entry
                skill = Skill(
                    name=entry_path.name,
                    description="",
                    body="",
                    root="user",
                    triggers=_DEFAULT_TRIGGERS,
                    est_tokens=0,
                    valid=False,
                    error="could not read SKILL.md",
                )
                user_skills.append(skill)
                continue
            raw = parse_skill_md(text, folder_name=entry_path.name, max_tokens=max_tokens)
            references, templates, scripts, resource_warnings = _discover_resources_path(
                entry_path,
                max_tokens=max_tokens,
            )
            user_skills.append(
                replace(
                    raw,
                    root="user",
                    triggers=_DEFAULT_TRIGGERS,
                    references=references,
                    templates=templates,
                    scripts=scripts,
                    warnings=raw.warnings + resource_warnings,
                )
            )

    return merge_skills(builtin_skills, user_skills)
