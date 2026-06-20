"""Tests for skill discovery and parsing (design section 23)."""

from __future__ import annotations

import importlib.resources
import json
import tomllib
from pathlib import Path

import pytest

import shellpilot.skills.loader as loader
from shellpilot.skills.loader import (
    SKILL_FILENAME,
    discover_skills,
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


def _user_skill_dir(tmp_path: Path, folder: str = "alpha") -> Path:
    _user_skill(tmp_path, folder, make_skill_md(name=folder))
    return tmp_path / folder


def _only_user_skill(
    tmp_path: Path,
    folder: str = "alpha",
    *,
    max_tokens: int = MAX_TOKENS,
) -> Skill:
    skills = discover_skills(user_skills_dir=tmp_path, max_tokens=max_tokens)
    return next(s for s in skills if s.root == "user" and s.name == folder)


def _builtin_skills(*, max_tokens: int = 800) -> dict[str, Skill]:
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        max_tokens=max_tokens,
    )
    return {skill.name: skill for skill in skills if skill.root == "builtin"}


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


def test_loader_resource_constants_are_exact() -> None:
    assert loader.MAX_RESOURCE_BYTES == 64 * 1024
    assert loader.MAX_RESOURCES_PER_KIND == 16


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
        max_tokens=MAX_TOKENS,
    )
    assert [s for s in skills if s.root == "user"] == []


def test_discover_user_skills_multiple_alphabetical_order(tmp_path: Path) -> None:
    """Multiple user skills are returned in alphabetical order."""
    for name in ("zebra", "alpha", "mango"):
        _user_skill(tmp_path, name, make_skill_md(name=name, description=f"The {name} skill."))
    skills = discover_skills(
        user_skills_dir=tmp_path,
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
        max_tokens=MAX_TOKENS,
    )
    user_skills = [s for s in skills if s.root == "user"]
    assert len(user_skills) == 1
    assert user_skills[0].valid is False


def test_discover_builtin_root_resolves() -> None:
    """Builtin root resolves via importlib.resources and yields all shipped builtins."""
    skills = discover_skills(
        user_skills_dir=Path("/nonexistent/skills"),
        max_tokens=MAX_TOKENS,
    )
    builtin_names = [s.name for s in skills if s.root == "builtin"]
    assert builtin_names == [
        "context-management",
        "planning",
        "skill-authoring",
        "web-grounding",
    ]


def test_builtin_planning_skill_loads() -> None:
    """The builtin planning skill loads valid with tiny body and mode references."""
    planning = _builtin_skills()["planning"]
    assert planning.valid is True
    assert planning.triggers == (
        SkillTrigger.PLAN_PROPOSED,
        SkillTrigger.PLAN_ACTIVE,
        SkillTrigger.PLAN_BLOCKED,
    )
    assert planning.body == (
        "You are working with a harness-managed plan. "
        "Follow only the current plan-mode guidance injected below."
    )
    assert "approval" not in planning.body.lower()
    assert "update_plan" not in planning.body
    assert [reference.rel_path for reference in planning.references] == [
        "references/active.md",
        "references/blocked.md",
        "references/proposed.md",
    ]
    reference_triggers = {reference.name: reference.trigger for reference in planning.references}
    assert reference_triggers == {
        "active": SkillTrigger.PLAN_ACTIVE,
        "blocked": SkillTrigger.PLAN_BLOCKED,
        "proposed": SkillTrigger.PLAN_PROPOSED,
    }
    assert [template.rel_path for template in planning.templates] == [
        "templates/blocker.md",
        "templates/plan.md",
        "templates/revised-plan.md",
    ]
    assert all(template.trigger is None for template in planning.templates)
    assert planning.scripts == ()
    ref_texts = {r.name: r.text for r in planning.references}
    assert "Every step is an action" in ref_texts["proposed"]
    assert "the final summary" in ref_texts["proposed"]
    assert "in the same turn" in ref_texts["active"]
    assert "The final summary is requested automatically" in ref_texts["active"]


def test_builtin_trigger_map_and_resources_by_folder_name() -> None:
    builtins = _builtin_skills()

    assert builtins["context-management"].triggers == (SkillTrigger.ALWAYS_ON,)
    assert builtins["context-management"].est_tokens <= 180
    assert [reference.rel_path for reference in builtins["context-management"].references] == [
        "references/context-budgeting.md",
        "references/file-triage.md",
    ]
    assert all(reference.trigger is None for reference in builtins["context-management"].references)
    assert builtins["context-management"].templates == ()
    assert builtins["context-management"].scripts == ()

    assert builtins["web-grounding"].triggers == (SkillTrigger.WEB_ENABLED,)
    assert "available does not mean use web" in builtins["web-grounding"].body
    assert "cite sources" in builtins["web-grounding"].body
    assert "approval" in builtins["web-grounding"].body
    assert "web_fetch" in builtins["web-grounding"].body
    assert "leads, not evidence" in builtins["web-grounding"].body
    assert "separate search" in builtins["web-grounding"].body
    assert "official" in builtins["web-grounding"].body
    assert "fetch only URLs from the search results" in builtins["web-grounding"].body
    assert "blocked or fails" in builtins["web-grounding"].body
    assert "don't assume the version" in builtins["web-grounding"].body
    assert builtins["web-grounding"].est_tokens <= 340
    assert builtins["web-grounding"].references == ()
    assert builtins["web-grounding"].templates == ()
    assert builtins["web-grounding"].scripts == ()

    assert builtins["skill-authoring"].triggers == (SkillTrigger.ENABLED,)
    assert [reference.rel_path for reference in builtins["skill-authoring"].references] == [
        "references/resource-routing.md",
        "references/skill-anatomy.md",
        "references/trigger-writing.md",
    ]
    assert all(reference.trigger is None for reference in builtins["skill-authoring"].references)
    assert [template.rel_path for template in builtins["skill-authoring"].templates] == [
        "templates/SKILL.md",
        "templates/skill-eval.md",
    ]
    assert all(template.trigger is None for template in builtins["skill-authoring"].templates)
    assert builtins["skill-authoring"].scripts == ()


def test_builtin_layout_loads_with_importlib_resources() -> None:
    root = importlib.resources.files("shellpilot.skills.builtin")
    expected_files = {
        "context-management": {
            "SKILL.md",
            "references/context-budgeting.md",
            "references/file-triage.md",
        },
        "planning": {
            "SKILL.md",
            "references/active.md",
            "references/blocked.md",
            "references/proposed.md",
            "templates/blocker.md",
            "templates/plan.md",
            "templates/revised-plan.md",
        },
        "skill-authoring": {
            "SKILL.md",
            "references/resource-routing.md",
            "references/skill-anatomy.md",
            "references/trigger-writing.md",
            "templates/SKILL.md",
            "templates/skill-eval.md",
        },
        "web-grounding": {"SKILL.md"},
    }

    for folder, rel_paths in expected_files.items():
        skill_dir = root / folder
        assert skill_dir.is_dir()
        for rel_path in rel_paths:
            resource = skill_dir
            for part in rel_path.split("/"):
                resource = resource / part
            assert resource.is_file(), f"missing {folder}/{rel_path}"
            assert resource.read_text(encoding="utf-8").strip()


def test_builtin_package_artifact_glob_stays_markdown_only() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["artifacts"] == ["shellpilot/skills/builtin/**/*.md"]


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
    """Guards the package layout in CI: builtin SKILL.md files are resources."""

    root = importlib.resources.files("shellpilot.skills.builtin")
    for name in ("context-management", "planning", "skill-authoring", "web-grounding"):
        skill_file = root / name / SKILL_FILENAME
        assert skill_file.is_file()
        text = skill_file.read_text(encoding="utf-8")
        assert text.startswith("---")


def test_discover_valid_references_and_templates_ordered(tmp_path: Path) -> None:
    """Direct markdown resources are discovered in deterministic stem order."""
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    templates = skill_dir / "templates"
    refs.mkdir()
    templates.mkdir()
    (refs / "zeta.md").write_text("Z reference", encoding="utf-8")
    (refs / "alpha.md").write_text("A reference", encoding="utf-8")
    (templates / "plan.md").write_text("Plan template", encoding="utf-8")
    (templates / "draft.md").write_text("Draft template", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert [r.name for r in skill.references] == ["alpha", "zeta"]
    assert [r.rel_path for r in skill.references] == ["references/alpha.md", "references/zeta.md"]
    assert [r.kind for r in skill.references] == ["reference", "reference"]
    assert [t.name for t in skill.templates] == ["draft", "plan"]
    assert [t.rel_path for t in skill.templates] == ["templates/draft.md", "templates/plan.md"]
    assert [t.kind for t in skill.templates] == ["template", "template"]
    assert skill.references[0].text == "A reference"
    assert skill.templates[0].text == "Draft template"
    assert skill.references[0].est_tokens == loader.estimate_tokens("A reference")
    assert skill.warnings == ()


def test_discover_ignores_non_markdown_resources(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "keep.md").write_text("Keep", encoding="utf-8")
    (refs / "ignore.txt").write_text("Ignore", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert [r.name for r in skill.references] == ["keep"]


def test_discover_ignores_nested_resource_subdirs(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    nested = refs / "nested"
    nested.mkdir(parents=True)
    (nested / "ignore.md").write_text("Ignore", encoding="utf-8")
    (refs / "keep.md").write_text("Keep", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert [r.name for r in skill.references] == ["keep"]


def test_discover_caps_resources_per_kind_with_exact_warning(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    templates = skill_dir / "templates"
    refs.mkdir()
    templates.mkdir()
    for index in range(18):
        (refs / f"ref-{index:02d}.md").write_text(f"Reference {index}", encoding="utf-8")
    for index in range(17):
        (templates / f"template-{index:02d}.md").write_text(f"Template {index}", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert len(skill.references) == 16
    assert len(skill.templates) == 16
    assert skill.references[-1].name == "ref-15"
    assert skill.templates[-1].name == "template-15"
    assert skill.warnings == (
        "references: found 18 markdown resources; loaded first 16 sorted by name",
        "templates: found 17 markdown resources; loaded first 16 sorted by name",
    )


def test_discover_oversized_resource_byte_capped_then_token_truncated(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "large.md").write_bytes(b"a" * ((64 * 1024) + 2048))

    skill = _only_user_skill(tmp_path, max_tokens=32)

    resource = skill.references[0]
    assert resource.name == "large"
    assert resource.text.startswith("a" * 128)
    assert "... [truncated " in resource.text
    assert "truncated 65408 chars" in resource.text
    assert len(resource.text) < 64 * 1024


def test_discover_resources_do_not_use_whole_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "bounded.md").write_text("Bounded read", encoding="utf-8")

    def fail_read_bytes(self: Path) -> bytes:
        if self.name == "bounded.md":
            raise AssertionError("resource discovery must not read whole files")
        return original_read_bytes(self)

    original_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    skill = _only_user_skill(tmp_path)

    assert [resource.name for resource in skill.references] == ["bounded"]
    assert skill.references[0].text == "Bounded read"


def test_discover_ignores_disallowed_top_level_dirs(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    docs = skill_dir / "docs"
    docs.mkdir()
    (docs / "ignore.md").write_text("Ignore", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert skill.references == ()
    assert skill.templates == ()
    assert skill.warnings == ()


def test_discover_rejects_resource_symlink_escape_with_warning(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("Outside", encoding="utf-8")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "escape.md").symlink_to(outside)

    skill = _only_user_skill(tmp_path)

    assert skill.references == ()
    assert skill.warnings == ("references/escape.md: skipped unsafe path",)


def test_discover_invalid_utf8_resource_is_warning_not_fatal(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "binary.md").write_bytes(b"\xff\xfe\xfd")

    skill = _only_user_skill(tmp_path)

    assert skill.references == ()
    assert skill.warnings == ("references/binary.md: could not decode resource",)


def test_discover_unreadable_resource_dir_is_warning_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    refs = skill_dir / "references"
    refs.mkdir()

    def fail_iterdir(self: Path) -> object:
        if self == refs:
            raise OSError("blocked")
        return original_iterdir(self)

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    skill = _only_user_skill(tmp_path)

    assert skill.references == ()
    assert skill.warnings == ("references/: could not read resources",)


def test_discover_valid_script_manifest(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "doctor.py").write_text("print('ok')\n", encoding="utf-8")
    (scripts / "manifest.json").write_text(
        """
[
  {
    "name": "doctor",
    "entry": "doctor.py",
    "description": "Check environment.",
    "mode": "read",
    "timeout_seconds": 30
  }
]
""".strip(),
        encoding="utf-8",
    )

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    script = skill.scripts[0]
    assert script.name == "doctor"
    assert script.entry == "scripts/doctor.py"
    assert script.description == "Check environment."
    assert script.mode == "read"
    assert script.timeout_seconds == 30
    assert script.valid is True
    assert script.error == ""
    assert skill.warnings == ()


@pytest.mark.parametrize(
    ("manifest_entry", "expected_error"),
    [
        (
            {
                "name": "bad-mode",
                "entry": "tool.py",
                "description": "Bad mode.",
                "mode": "execute",
                "timeout_seconds": 10,
            },
            "mode must be 'read' or 'write'",
        ),
        (
            {
                "name": "missing-description",
                "entry": "tool.py",
                "mode": "read",
                "timeout_seconds": -1,
            },
            "missing required key: description",
        ),
        (
            {
                "name": "bad-timeout",
                "entry": "tool.py",
                "description": "Bad timeout.",
                "mode": "read",
                "timeout_seconds": "10",
            },
            "timeout_seconds must be a positive int",
        ),
        (
            {
                "name": "zero-timeout",
                "entry": "tool.py",
                "description": "Zero timeout.",
                "mode": "read",
                "timeout_seconds": 0,
            },
            "timeout_seconds must be a positive int",
        ),
        (
            {
                "name": "missing-entry",
                "entry": "missing.py",
                "description": "Missing entry.",
                "mode": "read",
                "timeout_seconds": 10,
            },
            "entry does not exist under scripts/: missing.py",
        ),
    ],
)
def test_discover_invalid_script_manifest_entries(
    tmp_path: Path,
    manifest_entry: dict[str, object],
    expected_error: str,
) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    (scripts / "manifest.json").write_text(
        json.dumps([manifest_entry]),
        encoding="utf-8",
    )

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    script = skill.scripts[0]
    assert script.valid is False
    assert script.error == expected_error


@pytest.mark.parametrize(
    ("entry", "expected_error"),
    [
        ("../outside.py", "entry must be a bare relative filename"),
        ("/tmp/outside.py", "entry must be a bare relative filename"),
        ("nested/tool.py", "entry must be a bare relative filename"),
    ],
)
def test_discover_rejects_traversal_absolute_and_nested_script_entries(
    tmp_path: Path,
    entry: str,
    expected_error: str,
) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "name": "unsafe",
                    "entry": entry,
                    "description": "Unsafe entry.",
                    "mode": "read",
                    "timeout_seconds": 10,
                }
            ]
        ),
        encoding="utf-8",
    )

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    assert skill.scripts[0].valid is False
    assert skill.scripts[0].error == expected_error


def test_discover_rejects_script_symlink_escape(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "escape.py").symlink_to(outside)
    (scripts / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "name": "escape",
                    "entry": "escape.py",
                    "description": "Unsafe symlink.",
                    "mode": "read",
                    "timeout_seconds": 10,
                }
            ]
        ),
        encoding="utf-8",
    )

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    assert skill.scripts[0].valid is False
    assert skill.scripts[0].error == "entry path escapes skill root: escape.py"


def test_discover_rejects_script_symlink_outside_scripts_dir(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    inside_root = skill_dir / "tool.py"
    inside_root.write_text("print('not under scripts')\n", encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "alias.py").symlink_to(inside_root)
    (scripts / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "name": "alias",
                    "entry": "alias.py",
                    "description": "Unsafe alias.",
                    "mode": "read",
                    "timeout_seconds": 10,
                }
            ]
        ),
        encoding="utf-8",
    )

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    assert skill.scripts[0].valid is False
    assert skill.scripts[0].error == "entry path escapes scripts/: alias.py"


def test_discover_malformed_script_manifest_creates_invalid_placeholder(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "manifest.json").write_text("{not valid json", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    script = skill.scripts[0]
    assert script.name == "manifest"
    assert script.entry == "scripts/manifest.json"
    assert script.description == ""
    assert script.mode == "read"
    assert script.timeout_seconds == 0
    assert script.valid is False
    assert script.error.startswith("malformed scripts/manifest.json:")


def test_discover_invalid_utf8_script_manifest_creates_invalid_placeholder(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "manifest.json").write_bytes(b"\xff\xfe\xfd")

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    assert skill.scripts[0].valid is False
    assert skill.scripts[0].error.startswith("malformed scripts/manifest.json:")


def test_discover_rejects_manifest_symlink_outside_scripts_dir(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    root_manifest = skill_dir / "manifest.json"
    root_manifest.write_text("[]", encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "manifest.json").symlink_to(root_manifest)

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == 1
    assert skill.scripts[0].valid is False
    assert skill.scripts[0].error == "scripts/manifest.json: skipped unsafe path"


def test_discover_unreadable_scripts_dir_is_warning_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()

    def fail_iterdir(self: Path) -> object:
        if self == scripts:
            raise OSError("blocked")
        return original_iterdir(self)

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    skill = _only_user_skill(tmp_path)

    assert skill.scripts == ()
    assert skill.warnings == ("scripts/: could not read scripts directory; scripts ignored",)


def test_discover_scripts_without_manifest_ignored_with_warning(tmp_path: Path) -> None:
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "doctor.py").write_text("print('ok')\n", encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert skill.scripts == ()
    assert skill.warnings == ("scripts/: contains files but no manifest.json; scripts ignored",)


def test_user_skills_default_to_enabled_trigger(tmp_path: Path) -> None:
    _user_skill(tmp_path, "ordinary", make_skill_md(name="ordinary"))
    skill = _only_user_skill(tmp_path, "ordinary")
    assert skill.triggers == (SkillTrigger.ENABLED,)


def test_new_builtin_names_are_reserved(tmp_path: Path) -> None:
    _user_skill(tmp_path, "skill-authoring", make_skill_md(name="skill-authoring"))

    skills = discover_skills(user_skills_dir=tmp_path, max_tokens=800)

    collision = next(s for s in skills if s.root == "user" and s.name == "skill-authoring")
    assert collision.valid is False
    assert collision.error == "reserved builtin name"


# ---------------------------------------------------------------------------
# Fix 1: manifest.json byte-cap and entry-count cap (boot-DoS hardening)
# ---------------------------------------------------------------------------


def test_discover_oversized_manifest_skipped_gracefully(tmp_path: Path) -> None:
    """A manifest.json exceeding MAX_RESOURCE_BYTES is treated as invalid and no scripts loaded.

    The manifest is valid JSON but larger than the byte cap; the loader must
    cap the read and return an invalid placeholder — it must NOT load scripts
    from the oversized file.
    """
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    # Build a manifest that is valid JSON but exceeds MAX_RESOURCE_BYTES.
    # A list of dicts with large description strings yields a file > 64 KiB.
    many_entries = [
        {
            "name": f"tool-{i:03d}",
            "entry": "tool.py",
            "description": "x" * 4000,
            "mode": "read",
            "timeout_seconds": 10,
        }
        for i in range(20)
    ]
    oversized_json = json.dumps(many_entries).encode("utf-8")
    assert len(oversized_json) > loader.MAX_RESOURCE_BYTES, (
        f"test setup: expected oversized JSON, got {len(oversized_json)} bytes"
    )
    (scripts / "manifest.json").write_bytes(oversized_json)

    skill = _only_user_skill(tmp_path)

    # Must not crash; oversized manifest treated as invalid — one invalid placeholder.
    assert len(skill.scripts) == 1
    script = skill.scripts[0]
    assert script.valid is False
    assert script.entry == "scripts/manifest.json"


def test_discover_manifest_entry_count_capped_at_max_resources_per_kind(tmp_path: Path) -> None:
    """A manifest with more than MAX_RESOURCES_PER_KIND entries yields at most that many scripts."""
    skill_dir = _user_skill_dir(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    entry_count = loader.MAX_RESOURCES_PER_KIND + 5
    entries = []
    for i in range(entry_count):
        fname = f"tool_{i:02d}.py"
        (scripts / fname).write_text(f"print({i!r})\n", encoding="utf-8")
        entries.append(
            {
                "name": f"tool-{i:02d}",
                "entry": fname,
                "description": f"Tool {i}.",
                "mode": "read",
                "timeout_seconds": 10,
            }
        )
    (scripts / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")

    skill = _only_user_skill(tmp_path)

    assert len(skill.scripts) == loader.MAX_RESOURCES_PER_KIND
    assert all(s.valid for s in skill.scripts)
