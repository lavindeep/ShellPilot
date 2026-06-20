"""Tests for skill_read tool (design section 23.4 — progressive disclosure).

All resolution is done against in-memory objects; no filesystem access.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.skills.model import Skill, SkillResource, SkillTrigger
from shellpilot.tools.base import ToolContext, validate_args
from shellpilot.tools.skill_tools import make_skill_read_tool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _resource(
    name: str,
    text: str = "resource text",
    kind: str = "reference",
    trigger: SkillTrigger | None = SkillTrigger.ENABLED,
) -> SkillResource:
    return SkillResource(
        kind=kind,
        name=name,
        rel_path=f"references/{name}.md",
        text=text,
        est_tokens=2,
        trigger=trigger,
    )


def _template(name: str, text: str = "template text") -> SkillResource:
    return SkillResource(
        kind="template",
        name=name,
        rel_path=f"templates/{name}.md",
        text=text,
        est_tokens=2,
        trigger=None,  # templates have no trigger → on_demand
    )


def _skill(
    name: str,
    references: tuple[SkillResource, ...] = (),
    templates: tuple[SkillResource, ...] = (),
) -> Skill:
    return Skill(
        name=name,
        description=f"Skill {name}",
        body="body",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=1,
        references=references,
        templates=templates,
    )


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, max_result_tokens=2000)


# ---------------------------------------------------------------------------
# 2. Valid (skill, resource) — reference
# ---------------------------------------------------------------------------


def test_skill_read_valid_reference(tmp_path: Path) -> None:
    """Valid (skill, reference) → success=True, content is the resource text."""
    ref = _resource("api-guide", text="Use this API carefully.")
    skill = _skill("my-skill", references=(ref,))
    spec = make_skill_read_tool((skill,))

    result = spec.handler(_ctx(tmp_path), {"skill": "my-skill", "resource": "api-guide"})

    assert result.success is True
    assert result.content == "Use this API carefully."
    assert "my-skill" in result.summary
    assert "api-guide" in result.summary


# ---------------------------------------------------------------------------
# 3. Template is also readable
# ---------------------------------------------------------------------------


def test_skill_read_valid_template(tmp_path: Path) -> None:
    """Templates are readable the same way references are."""
    tmpl = _template("plan", text="Template contents here.")
    skill = _skill("my-skill", templates=(tmpl,))
    spec = make_skill_read_tool((skill,))

    result = spec.handler(_ctx(tmp_path), {"skill": "my-skill", "resource": "plan"})

    assert result.success is True
    assert result.content == "Template contents here."


# ---------------------------------------------------------------------------
# 4. Unknown skill → failure listing available skill names
# ---------------------------------------------------------------------------


def test_skill_read_unknown_skill(tmp_path: Path) -> None:
    """Unknown skill name → success=False, content lists available skill names."""
    skill_a = _skill("alpha")
    skill_b = _skill("beta")
    spec = make_skill_read_tool((skill_a, skill_b))

    result = spec.handler(_ctx(tmp_path), {"skill": "unknown-skill", "resource": "anything"})

    assert result.success is False
    assert "alpha" in result.content
    assert "beta" in result.content


# ---------------------------------------------------------------------------
# 5. Unknown resource → failure listing that skill's resource names
# ---------------------------------------------------------------------------


def test_skill_read_unknown_resource(tmp_path: Path) -> None:
    """Unknown resource name → success=False, content lists available resource names."""
    ref = _resource("api-guide")
    tmpl = _template("plan")
    skill = _skill("my-skill", references=(ref,), templates=(tmpl,))
    spec = make_skill_read_tool((skill,))

    result = spec.handler(_ctx(tmp_path), {"skill": "my-skill", "resource": "no-such-doc"})

    assert result.success is False
    assert "api-guide" in result.content
    assert "plan" in result.content


# ---------------------------------------------------------------------------
# 6. Path-like args match nothing → clean failure, no exception, no file access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skill_arg,resource_arg",
    [
        ("my-skill", "../secret"),
        ("my-skill", "references/api-guide.md"),
        ("../escape", "api-guide"),
        ("/abs/path", "api-guide"),
    ],
)
def test_skill_read_path_like_args_fail_cleanly(
    tmp_path: Path, skill_arg: str, resource_arg: str
) -> None:
    """Path-like args resolve to nothing — no exception, no file access."""
    ref = _resource("api-guide")
    skill = _skill("my-skill", references=(ref,))
    spec = make_skill_read_tool((skill,))

    # Must not raise
    result = spec.handler(_ctx(tmp_path), {"skill": skill_arg, "resource": resource_arg})

    assert result.success is False


# ---------------------------------------------------------------------------
# 7. A skill not in the provided list is not readable
# ---------------------------------------------------------------------------


def test_skill_read_not_in_list_is_unreadable(tmp_path: Path) -> None:
    """A skill name not in the closed-over list returns failure."""
    skill = _skill("included")
    spec = make_skill_read_tool((skill,))

    result = spec.handler(_ctx(tmp_path), {"skill": "not-included", "resource": "anything"})

    assert result.success is False


def test_skill_read_invalid_skill_present_is_unreadable(tmp_path: Path) -> None:
    """An invalid/reserved skill PRESENT in the list is unreadable and unadvertised.

    discover_skills returns valid + invalid skills, and the runtime closes the
    tool over the full set, so the handler itself must refuse invalid skills —
    otherwise skill_read becomes the one path that leaks invalid-skill content.
    """
    secret = _resource("secret", text="SECRET CONTENT", trigger=None)
    bad = replace(
        _skill("planning", references=(secret,)),
        valid=False,
        error="reserved builtin name",
    )
    good = _skill("real-skill", references=(_resource("guide", text="ok"),))
    spec = make_skill_read_tool((bad, good))

    result = spec.handler(_ctx(tmp_path), {"skill": "planning", "resource": "secret"})

    assert result.success is False
    assert "SECRET CONTENT" not in result.content
    # The invalid skill is not advertised in the available-skills listing
    # (its name may still echo back as the unknown query — that's fine).
    assert "Available skills: real-skill" in result.content


# ---------------------------------------------------------------------------
# 8. validate_args integration
# ---------------------------------------------------------------------------


def test_skill_read_validate_args_missing_skill() -> None:
    """Missing 'skill' arg → validate_args returns an error string."""
    spec = make_skill_read_tool(())
    error = validate_args(spec, {"resource": "doc"})
    assert error is not None
    assert "skill" in error


def test_skill_read_validate_args_missing_resource() -> None:
    """Missing 'resource' arg → validate_args returns an error string."""
    spec = make_skill_read_tool(())
    error = validate_args(spec, {"skill": "some-skill"})
    assert error is not None
    assert "resource" in error


def test_skill_read_validate_args_unknown_extra() -> None:
    """Unknown extra arg → validate_args returns an error string."""
    spec = make_skill_read_tool(())
    error = validate_args(spec, {"skill": "s", "resource": "r", "bogus": "x"})
    assert error is not None
    assert "bogus" in error


def test_skill_read_validate_args_valid_passes() -> None:
    """Valid args with both required fields → validate_args returns None."""
    spec = make_skill_read_tool(())
    error = validate_args(spec, {"skill": "s", "resource": "r"})
    assert error is None


# ---------------------------------------------------------------------------
# 10. Spec safety metadata
# ---------------------------------------------------------------------------


def test_skill_read_spec_metadata() -> None:
    """skill_read must be SideEffect.NONE and RiskLevel.LOW."""
    spec = make_skill_read_tool(())
    assert spec.side_effect is SideEffect.NONE
    assert spec.default_risk is RiskLevel.LOW
