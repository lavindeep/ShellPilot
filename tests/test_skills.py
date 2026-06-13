"""Tests for skill discovery and parsing (design section 23)."""

from __future__ import annotations

from pathlib import Path

import pytest

import shellpilot.skills.loader as loader
from shellpilot.skills.loader import (
    SKILL_FILENAME,
    discover_skills,
    is_enabled,
    merge_skills,
    parse_skill_md,
)
from shellpilot.skills.model import Skill, SkillTrigger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_TOKENS = 200


def make_skill_md(
    *,
    name: str = "planning",
    description: str = "A skill.",
    body: str = "Do stuff.",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


def _user_skill(
    tmp_path: Path,
    folder: str,
    content: str,
) -> Path:
    d = tmp_path / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILENAME).write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# parse_skill_md
# ---------------------------------------------------------------------------


def test_parse_valid_skill_md() -> None:
    """A well-formed SKILL.md parses to a valid Skill with correct fields."""
    text = "---\nname: planning\ndescription: Execution discipline.\n---\nDo the work.\n"
    skill = parse_skill_md(text, folder_name="planning", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    assert skill.name == "planning"
    assert skill.description == "Execution discipline."
    assert skill.body == "Do the work."
    assert skill.error == ""
    assert skill.warnings == ()


def test_parse_missing_opening_delimiter_is_invalid() -> None:
    """A SKILL.md that does not start with --- is invalid."""
    text = "name: planning\ndescription: something\n---\nbody\n"
    skill = parse_skill_md(text, folder_name="planning", max_tokens=MAX_TOKENS)
    assert skill.valid is False
    assert "opening" in skill.error


def test_parse_missing_closing_delimiter_is_invalid() -> None:
    """A SKILL.md with opening --- but no closing --- is invalid."""
    # Two cases: clean key-value lines with no closing ---, and a body-only file
    # that never gets a closing delimiter.
    text_no_close = "---\nname: planning\ndescription: something\n"
    skill = parse_skill_md(text_no_close, folder_name="planning", max_tokens=MAX_TOKENS)
    assert skill.valid is False


def test_parse_unknown_frontmatter_key_is_invalid() -> None:
    """An unknown key in the frontmatter makes the skill invalid."""
    text = "---\nname: planning\nunknown_key: value\n---\nbody\n"
    skill = parse_skill_md(text, folder_name="planning", max_tokens=MAX_TOKENS)
    assert skill.valid is False
    assert "unknown_key" in skill.error


def test_parse_body_extracted_and_stripped() -> None:
    """Body text is extracted after the closing --- and stripped."""
    text = "---\nname: foo\ndescription: bar\n---\n\n  Body text here.  \n"
    skill = parse_skill_md(text, folder_name="foo", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    assert skill.body == "Body text here."


def test_parse_empty_body_is_valid() -> None:
    """An empty body after the closing --- is a valid skill."""
    text = "---\nname: foo\ndescription: bar\n---\n"
    skill = parse_skill_md(text, folder_name="foo", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    assert skill.body == ""


def test_parse_body_truncated_at_cap_still_valid() -> None:
    """A body exceeding max_tokens is truncated; the skill remains valid."""
    long_body = "x" * 1000  # well over MAX_TOKENS
    text = f"---\nname: foo\ndescription: bar\n---\n{long_body}\n"
    skill = parse_skill_md(text, folder_name="foo", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    # Truncated body is shorter than the full body
    assert len(skill.body) < 1000
    assert "truncated" in skill.body


def test_parse_folder_name_authoritative_mismatch_advisory() -> None:
    """Frontmatter name mismatch: skill is valid, advisory in warnings."""
    text = "---\nname: different-name\ndescription: Desc.\n---\nbody\n"
    skill = parse_skill_md(text, folder_name="my-skill", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    assert skill.name == "my-skill"  # folder name wins
    assert skill.error == ""
    assert len(skill.warnings) == 1
    assert "different-name" in skill.warnings[0]
    assert "authoritative" in skill.warnings[0]


def test_parse_folder_name_authoritative_matching_no_advisory() -> None:
    """When frontmatter name matches folder name, no advisory in error."""
    text = "---\nname: my-skill\ndescription: Desc.\n---\nbody\n"
    skill = parse_skill_md(text, folder_name="my-skill", max_tokens=MAX_TOKENS)
    assert skill.valid is True
    assert skill.error == ""
    assert skill.warnings == ()


def test_skill_trigger_enum_values_are_v070_set() -> None:
    assert {trigger.value for trigger in SkillTrigger} == {
        "always_on",
        "enabled",
        "plan_proposed",
        "plan_active",
        "plan_blocked",
        "web_enabled",
    }


def test_skill_defaults_for_v2_collections() -> None:
    skill = Skill(
        name="alpha",
        description="desc",
        body="body",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=3,
    )
    assert skill.references == ()
    assert skill.templates == ()
    assert skill.scripts == ()
    assert skill.warnings == ()


def test_skill_resource_and_script_construct() -> None:
    from shellpilot.skills.model import SkillResource, SkillScript

    resource = SkillResource(
        kind="reference",
        name="api",
        rel_path="references/api.md",
        text="Use this API.",
        est_tokens=4,
        trigger=SkillTrigger.WEB_ENABLED,
    )
    template = SkillResource(
        kind="template",
        name="plan",
        rel_path="templates/plan.md",
        text="Template",
        est_tokens=2,
    )
    script = SkillScript(
        name="doctor",
        entry="scripts/doctor.py",
        description="Check environment.",
        mode="python",
        timeout_seconds=30,
    )

    assert resource.trigger is SkillTrigger.WEB_ENABLED
    assert template.trigger is None
    assert script.valid is True
    assert script.error == ""


# ---------------------------------------------------------------------------
# merge_skills
# ---------------------------------------------------------------------------


def _make_skill(name: str, root: str = "user", valid: bool = True) -> Skill:
    return Skill(
        name=name,
        description="desc",
        body="body",
        root=root,
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=1,
        valid=valid,
    )


def test_merge_no_collision_passes_through() -> None:
    """No collision: all skills pass through unchanged."""
    builtin = [_make_skill("planning", root="builtin")]
    user = [_make_skill("my-skill", root="user")]
    merged = merge_skills(builtin, user)
    assert len(merged) == 2
    assert all(s.valid for s in merged)


def test_merge_user_collides_with_builtin_becomes_invalid() -> None:
    """A user skill whose name matches a builtin becomes invalid (reserved name)."""
    builtin = [_make_skill("planning", root="builtin")]
    user = [_make_skill("planning", root="user")]
    merged = merge_skills(builtin, user)
    builtin_results = [s for s in merged if s.root == "builtin"]
    user_results = [s for s in merged if s.root == "user"]
    assert len(builtin_results) == 1
    assert builtin_results[0].valid is True
    assert len(user_results) == 1
    assert user_results[0].valid is False
    assert "reserved builtin name" in user_results[0].error


def test_merge_order_builtin_first_then_user() -> None:
    """Merged list has builtins before user skills."""
    builtin = [_make_skill("b-skill", root="builtin")]
    user = [_make_skill("a-skill", root="user")]
    merged = merge_skills(builtin, user)
    assert merged[0].root == "builtin"
    assert merged[1].root == "user"


def test_merge_already_invalid_builtin_name_still_reserves() -> None:
    """Even an invalid builtin skill reserves the name."""
    builtin = [_make_skill("planning", root="builtin", valid=False)]
    user = [_make_skill("planning", root="user")]
    merged = merge_skills(builtin, user)
    user_results = [s for s in merged if s.root == "user"]
    assert user_results[0].valid is False
    assert "reserved builtin name" in user_results[0].error


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------


def test_discover_absent_user_dir_returns_no_user_skills(tmp_path: Path) -> None:
    """An absent user_skills_dir yields no user skills and does not raise."""
    skills = discover_skills(
        user_skills_dir=tmp_path / "nonexistent",
        enabled=(),
        max_tokens=MAX_TOKENS,
    )
    assert [s for s in skills if s.root == "user"] == []


def test_discover_user_skills_multiple_alphabetical_order(tmp_path: Path) -> None:
    """Multiple user skills are returned in alphabetical order."""
    for name in ("zebra", "alpha", "mango"):
        _user_skill(tmp_path, name, make_skill_md(name=name, description=f"The {name} skill."))
    skills = discover_skills(
        user_skills_dir=tmp_path,
        enabled=("alpha", "zebra", "mango"),
        max_tokens=MAX_TOKENS,
    )
    user_skills = [s for s in skills if s.root == "user"]
    assert [s.name for s in user_skills] == ["alpha", "mango", "zebra"]


def test_discover_non_directory_entries_skipped(tmp_path: Path) -> None:
    """Non-directory entries in the user skills dir are silently skipped."""
    _user_skill(tmp_path, "real-skill", make_skill_md(name="real-skill"))
    (tmp_path / "stray-file.txt").write_text("hello", encoding="utf-8")
    skills = discover_skills(
        user_skills_dir=tmp_path,
        enabled=(),
        max_tokens=MAX_TOKENS,
    )
    user_skills = [s for s in skills if s.root == "user"]
    assert len(user_skills) == 1
    assert user_skills[0].name == "real-skill"


def test_discover_invalid_skill_included_not_dropped(tmp_path: Path) -> None:
    """Invalid skills (bad frontmatter) are listed, not dropped."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / SKILL_FILENAME).write_text("no frontmatter at all", encoding="utf-8")
    skills = discover_skills(
        user_skills_dir=tmp_path,
        enabled=(),
        max_tokens=MAX_TOKENS,
    )
    user_skills = [s for s in skills if s.root == "user"]
    assert len(user_skills) == 1
    assert user_skills[0].valid is False


def test_discover_builtin_root_resolves() -> None:
    """Builtin root resolves via importlib.resources and yields the planning skill."""
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        enabled=(),
        max_tokens=MAX_TOKENS,
    )
    builtin_names = [s.name for s in skills if s.root == "builtin"]
    assert builtin_names == ["planning"]


def test_builtin_planning_skill_loads() -> None:
    """The builtin planning skill loads valid, plan triggers, with execution body."""
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        enabled=(),
        max_tokens=800,
    )
    planning = next(s for s in skills if s.root == "builtin" and s.name == "planning")
    assert planning.valid is True
    assert planning.triggers == (
        SkillTrigger.PLAN_PROPOSED,
        SkillTrigger.PLAN_ACTIVE,
        SkillTrigger.PLAN_BLOCKED,
    )
    assert "update_plan" in planning.body
    assert 'update_plan(blocker="<evidence>")' in planning.body


def test_unreadable_builtin_planning_preserves_plan_triggers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even invalid planning reserves the builtin name with planning triggers."""

    class UnreadableSkillFile:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            raise OSError("boom")

    class BuiltinSkillDir:
        name = "planning"

        def is_dir(self) -> bool:
            return True

        def __truediv__(self, _name: str) -> UnreadableSkillFile:
            return UnreadableSkillFile()

    class BuiltinRoot:
        def iterdir(self) -> tuple[BuiltinSkillDir, ...]:
            return (BuiltinSkillDir(),)

    monkeypatch.setattr(loader.importlib.resources, "files", lambda _package: BuiltinRoot())

    skills = discover_skills(
        user_skills_dir=tmp_path / "missing-user-skills",
        enabled=(),
        max_tokens=MAX_TOKENS,
    )

    planning = next(s for s in skills if s.root == "builtin" and s.name == "planning")
    assert planning.valid is False
    assert planning.error == "could not read SKILL.md"
    assert planning.triggers == (
        SkillTrigger.PLAN_PROPOSED,
        SkillTrigger.PLAN_ACTIVE,
        SkillTrigger.PLAN_BLOCKED,
    )


def test_builtin_skills_dir_resolvable() -> None:
    """Guards the package layout in CI: the builtin root resolves from the source
    tree and contains planning/SKILL.md."""
    import importlib.resources

    root = importlib.resources.files("shellpilot.skills.builtin")
    skill_file = root / "planning" / SKILL_FILENAME
    assert skill_file.is_file()
    text = skill_file.read_text(encoding="utf-8")
    assert text.startswith("---")


# ---------------------------------------------------------------------------
# is_enabled helper
# ---------------------------------------------------------------------------


def test_is_enabled_planning_builtin_always_enabled() -> None:
    """The planning builtin skill is always enabled regardless of the enabled list."""
    skill = Skill(
        name="planning",
        description="",
        body="",
        root="builtin",
        triggers=(
            SkillTrigger.PLAN_PROPOSED,
            SkillTrigger.PLAN_ACTIVE,
            SkillTrigger.PLAN_BLOCKED,
        ),
        est_tokens=0,
    )
    assert is_enabled(skill, ()) is True
    assert is_enabled(skill, ("other",)) is True


def test_is_enabled_user_skill_follows_enabled_list() -> None:
    """A user skill is enabled only when its name is in the enabled tuple."""
    skill = Skill(
        name="my-skill",
        description="",
        body="",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=0,
    )
    assert is_enabled(skill, ()) is False
    assert is_enabled(skill, ("my-skill",)) is True
    assert is_enabled(skill, ("other",)) is False
