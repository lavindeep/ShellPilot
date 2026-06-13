"""Skill discovery and parsing (design section 23)."""

from __future__ import annotations

import importlib.resources
import importlib.resources.abc
from dataclasses import replace
from pathlib import Path

from shellpilot.runtime.budget import estimate_tokens, truncate_to_tokens
from shellpilot.skills.model import Skill, SkillTrigger
from shellpilot.skills.triggers import TriggerContext, any_fires

SKILL_FILENAME = "SKILL.md"

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
            skill = replace(raw, root="builtin", triggers=_triggers_for_skill_name(entry.name))
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
            user_skills.append(replace(raw, root="user", triggers=_DEFAULT_TRIGGERS))

    return merge_skills(builtin_skills, user_skills)
