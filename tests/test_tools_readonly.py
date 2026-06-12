"""Tests for the read-only tools in real temp directories."""

from pathlib import Path
from typing import Any

import pytest

from shellpilot.llm.messages import ToolCall
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.executor import ToolExecutor
from shellpilot.tools.base import ToolContext, WorkspaceBoundaryError, resolve_in_workspace
from shellpilot.tools.environment import ENV_INFO
from shellpilot.tools.filesystem import LIST_DIR, READ_FILE
from shellpilot.tools.registry import ToolRegistry
from shellpilot.tools.search import SEARCH_TEXT


def ctx(workspace: Path, allow_sensitive_reads: str = "ask") -> ToolContext:
    return ToolContext(
        workspace=workspace,
        max_result_tokens=2000,
        allow_sensitive_reads=allow_sensitive_reads,
    )


# -- read_file ----------------------------------------------------------------


def test_read_file_happy(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\nprint('there')\n")
    result = READ_FILE.handler(ctx(tmp_path), {"path": "hello.py"})
    assert result.success
    assert "print('hi')" in result.content
    assert "lines 1-2 of 2" in result.summary


def test_read_file_window(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join(f"line{i}" for i in range(1, 401)))
    result = READ_FILE.handler(
        ctx(tmp_path), {"path": "big.txt", "start_line": 100, "max_lines": 3}
    )
    assert result.success
    assert result.content.splitlines() == ["line100", "line101", "line102"]
    assert result.truncated  # window smaller than file


def test_read_file_missing(tmp_path: Path) -> None:
    result = READ_FILE.handler(ctx(tmp_path), {"path": "nope.txt"})
    assert not result.success


def test_read_file_refuses_binary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02data")
    result = READ_FILE.handler(ctx(tmp_path), {"path": "blob.bin"})
    assert not result.success
    assert "binary" in result.summary


def test_read_file_outside_workspace_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    with pytest.raises(WorkspaceBoundaryError):
        READ_FILE.handler(ctx(workspace), {"path": "../secret.txt"})


def test_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "alias.txt").symlink_to(outside)
    with pytest.raises(WorkspaceBoundaryError):
        resolve_in_workspace(workspace, "alias.txt")


# -- list_dir -----------------------------------------------------------------


def test_list_dir_happy(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "a.py").write_text("x = 1")
    result = LIST_DIR.handler(ctx(tmp_path), {"path": "."})
    assert result.success
    assert "pkg/" in result.content
    assert "a.py" in result.content


def test_list_dir_on_file_fails(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    result = LIST_DIR.handler(ctx(tmp_path), {"path": "a.py"})
    assert not result.success


# -- search_text --------------------------------------------------------------


def test_search_finds_matches_and_skips_git(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def load_config():\n    pass\n")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "junk.txt").write_text("load_config here too")

    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "load_config"})
    assert result.success
    assert "src/app.py:1" in result.content
    assert ".git" not in result.content
    assert "1 matches" in result.summary


def test_search_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00load_config\x00")
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "load_config"})
    assert result.success
    assert result.content == ""


def test_search_empty_pattern_fails(tmp_path: Path) -> None:
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": ""})
    assert not result.success


def test_search_bounds_matches(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("needle\n" * 500)
    result = SEARCH_TEXT.handler(ctx(tmp_path), {"pattern": "needle"})
    assert result.truncated
    assert "100 matches" in result.summary


# -- env_info -----------------------------------------------------------------


def test_env_info_reports_environment(tmp_path: Path) -> None:
    result = ENV_INFO.handler(ctx(tmp_path), {})
    assert result.success
    assert "os:" in result.content
    assert "python:" in result.content
    assert str(tmp_path) in result.content


# -- sensitive-read classifiers (design section 15) ---------------------------


def test_read_file_classifier_flags_sensitive_path(tmp_path: Path) -> None:
    risk = READ_FILE.risk_for(ctx(tmp_path), {"path": ".env"})
    assert risk.risk is RiskLevel.HIGH
    assert risk.reasons and ".env" in risk.reasons[0]


def test_read_file_classifier_flags_nested_sensitive_path(tmp_path: Path) -> None:
    risk = READ_FILE.risk_for(ctx(tmp_path), {"path": "sub/.env.local"})
    assert risk.risk is RiskLevel.HIGH


def test_read_file_classifier_flags_ssh_directory_component(tmp_path: Path) -> None:
    risk = READ_FILE.risk_for(ctx(tmp_path), {"path": ".ssh/known_hosts"})
    assert risk.risk is RiskLevel.HIGH


def test_read_file_classifier_ignores_lookalikes(tmp_path: Path) -> None:
    assert READ_FILE.risk_for(ctx(tmp_path), {"path": "environment.py"}).risk is RiskLevel.LOW
    assert READ_FILE.risk_for(ctx(tmp_path), {"path": "secrets_test.py"}).risk is RiskLevel.LOW


def test_read_file_classifier_tolerates_bad_path(tmp_path: Path) -> None:
    # A path that escapes the workspace cannot resolve; classifier falls back to LOW.
    assert READ_FILE.risk_for(ctx(tmp_path), {"path": "../../.env"}).risk is RiskLevel.LOW


def test_search_text_classifier_gates_explicit_sensitive_path(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1")
    risk = SEARCH_TEXT.risk_for(ctx(tmp_path), {"pattern": "x", "path": ".env"})
    assert risk.risk is RiskLevel.HIGH


# -- sensitive read through the executor (the privacy gate) -------------------


class _SpyAsker:
    """Records approval requests and answers with a fixed verdict."""

    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.requests: list[ApprovalRequest] = []

    def __call__(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.approve


def _executor(
    tmp_path: Path,
    *,
    allow_sensitive_reads: str,
    ask_approval: Any = None,
    explain_purpose: Any = None,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(READ_FILE)
    registry.register(SEARCH_TEXT)
    return ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="balanced",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=ask_approval,
        explain_purpose=explain_purpose,
        allow_sensitive_reads=allow_sensitive_reads,
    )


def test_env_read_ask_mode_prompts_and_returns_content_when_approved(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret-value")
    asker = _SpyAsker(approve=True)
    executor = _executor(tmp_path, allow_sensitive_reads="ask", ask_approval=asker)

    outcome = executor.execute(ToolCall(name="read_file", arguments={"path": ".env"}))

    assert len(asker.requests) == 1
    request = asker.requests[0]
    assert request.risk is RiskLevel.HIGH
    assert request.kind == "tool"  # standard y/n prompt, not the typed-"run" command gate
    assert any(".env" in reason for reason in request.reasons)
    assert not request.purpose  # no model purpose round-trip for a sensitive read
    assert outcome.result is not None and outcome.result.success
    assert "secret-value" in outcome.result.content


def test_env_read_ask_mode_declined_returns_standard_declined(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret-value")
    asker = _SpyAsker(approve=False)
    executor = _executor(tmp_path, allow_sensitive_reads="ask", ask_approval=asker)

    outcome = executor.execute(ToolCall(name="read_file", arguments={"path": ".env"}))

    assert len(asker.requests) == 1
    assert outcome.result is not None and not outcome.result.success
    assert "declined" in outcome.result.summary
    assert "status: declined" in outcome.model_text


def test_env_read_never_mode_blocks_without_prompting(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret-value")
    asker = _SpyAsker(approve=True)
    executor = _executor(tmp_path, allow_sensitive_reads="never", ask_approval=asker)

    outcome = executor.execute(ToolCall(name="read_file", arguments={"path": ".env"}))

    assert asker.requests == []  # blocked without ever asking
    assert outcome.result is not None and not outcome.result.success
    assert "status: blocked" in outcome.model_text
    assert "Do not retry" in outcome.model_text


def test_env_read_always_mode_auto_returns_content(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret-value")
    asker = _SpyAsker(approve=False)  # would decline if asked
    executor = _executor(tmp_path, allow_sensitive_reads="always", ask_approval=asker)

    outcome = executor.execute(ToolCall(name="read_file", arguments={"path": ".env"}))

    assert asker.requests == []  # AUTO, never prompts
    assert outcome.result is not None and outcome.result.success
    assert "secret-value" in outcome.result.content


def test_plain_file_read_is_auto_no_prompt(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("nothing secret here")
    asker = _SpyAsker(approve=False)
    executor = _executor(tmp_path, allow_sensitive_reads="ask", ask_approval=asker)

    outcome = executor.execute(ToolCall(name="read_file", arguments={"path": "notes.txt"}))

    assert asker.requests == []  # regression pin: ordinary reads never prompt
    assert outcome.result is not None and outcome.result.success
    assert "nothing secret here" in outcome.result.content


def test_sensitive_read_never_calls_explain_purpose(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret-value")
    called = False

    def _explain(display: str, reasons: tuple[str, ...]) -> str:
        nonlocal called
        called = True
        return "should never run"

    asker = _SpyAsker(approve=True)
    executor = _executor(
        tmp_path, allow_sensitive_reads="ask", ask_approval=asker, explain_purpose=_explain
    )
    executor.execute(ToolCall(name="read_file", arguments={"path": ".env"}))

    assert not called  # no model purpose generation for a sensitive read


# -- search_text traversal skips sensitive files ------------------------------


def test_search_text_skips_sensitive_files_and_notes(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=needle")
    (tmp_path / "app.py").write_text("x = 'needle'\n")
    result = SEARCH_TEXT.handler(ctx(tmp_path, "ask"), {"pattern": "needle"})

    assert result.success
    assert "app.py:1" in result.content
    assert ".env:" not in result.content  # contents never read
    assert "skipped 1 sensitive file(s) (.env)" in result.content
    assert 'allow_sensitive_reads = "always"' in result.content


def test_search_text_always_mode_includes_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=needle")
    (tmp_path / "app.py").write_text("x = 'needle'\n")
    result = SEARCH_TEXT.handler(ctx(tmp_path, "always"), {"pattern": "needle"})

    assert result.success
    assert ".env:1" in result.content
    assert "skipped" not in result.content
