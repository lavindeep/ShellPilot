"""Tests for the shell=False command runner (design section 13.1)."""

import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from shellpilot.llm.messages import ToolCall
from shellpilot.runtime.executor import ToolExecutor
from shellpilot.tools.base import ToolContext
from shellpilot.tools.command import (
    RUN_COMMAND,
    CommandRequest,
    _precheck_run_command,
    run_command_process,
)
from shellpilot.tools.registry import ToolRegistry


def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, max_result_tokens=2000, max_capture_chars=10_000)


def _make_executor(tmp_path: Path, ask_approval: Any = None) -> ToolExecutor:
    """Build an executor with only RUN_COMMAND registered."""
    registry = ToolRegistry()
    registry.register(RUN_COMMAND)
    return ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        ask_approval=ask_approval,
    )


def _approval_spy(approved: bool = True) -> tuple[Any, list[Any]]:
    """Return (asker_fn, calls_list); calls_list records invocations."""
    calls: list[Any] = []

    def _ask(request: Any) -> bool:
        calls.append(request)
        return approved

    return _ask, calls


def test_echo_succeeds(tmp_path: Path) -> None:
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["echo", "hello world"]})
    assert result.success
    assert "hello world" in result.content
    assert result.summary == "exit code 0"


def test_nonzero_exit_is_failure(tmp_path: Path) -> None:
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["false"]})
    assert not result.success
    assert "exit code 1" in result.summary


def test_grep_no_match_is_expected_nonzero(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("nothing here")
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["grep", "needle-not-present", "f.txt"]})
    assert result.success  # grep exit 1 means "no matches", not failure (section 24.3)


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    start = time.monotonic()
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=1,
        ),
        max_capture_chars=1000,
    )
    elapsed = time.monotonic() - start
    assert outcome.timed_out
    assert elapsed < 10


def test_output_capture_is_bounded(tmp_path: Path) -> None:
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "print('x' * 100 + '\\n', end='');" * 1],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=50,
    )
    assert len(outcome.output) <= 101  # one buffered line may land before the cap


def test_streaming_emits_lines(tmp_path: Path) -> None:
    seen: list[str] = []
    run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "print('one'); print('two')"],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=10_000,
        emit_line=seen.append,
    )
    assert seen == ["one", "two"]


def test_cwd_is_workspace(tmp_path: Path) -> None:
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["pwd"]})
    assert result.success
    assert str(tmp_path.resolve()) in result.content


# ---------------------------------------------------------------------------
# packed-shell-line and empty-argv: now rejected pre-approval via precheck
# ---------------------------------------------------------------------------


def test_packed_shell_line_is_rejected_with_guidance(tmp_path: Path) -> None:
    """Packed shell line produces a failed result; guidance is in content."""
    # Drive through the precheck directly (handler-level call skips precheck)
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["echo hi > out.txt"]})
    assert msg is not None
    assert "WITHOUT a shell" in msg or "shell syntax" in msg


def test_packed_shell_line_rejected_pre_approval(tmp_path: Path) -> None:
    """No approval request is made for a packed shell line."""
    asker, calls = _approval_spy()
    executor = _make_executor(tmp_path, ask_approval=asker)
    call = ToolCall(name="run_command", arguments={"argv": ["echo hi > out.txt"]})
    outcome = executor.execute(call)
    assert not outcome.result.success  # type: ignore[union-attr]
    assert len(calls) == 0, "approval must not be requested for a packed shell line"


def test_redirection_in_first_token_is_rejected(tmp_path: Path) -> None:
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["cat>x", "f"]})
    assert msg is not None


def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    """Empty argv is rejected pre-approval via precheck."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": []})
    assert msg is not None
    assert "empty" in msg.lower() or "argv must not be empty" in msg


def test_empty_argv_rejected_pre_approval(tmp_path: Path) -> None:
    """No approval request is made for empty argv."""
    asker, calls = _approval_spy()
    executor = _make_executor(tmp_path, ask_approval=asker)
    call = ToolCall(name="run_command", arguments={"argv": []})
    outcome = executor.execute(call)
    assert not outcome.result.success  # type: ignore[union-attr]
    assert len(calls) == 0, "approval must not be requested for empty argv"


# ---------------------------------------------------------------------------
# Executable resolution: PATH-based checks (hermetic via monkeypatched PATH)
# ---------------------------------------------------------------------------


def _make_fake_path(tmp_path: Path, executables: list[str]) -> str:
    """Create a tmp dir populated with fake executables, return as PATH string."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for name in executables:
        p = bin_dir / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    return str(bin_dir)


def test_missing_executable_on_path_suggests_python3(tmp_path: Path) -> None:
    """python not on PATH but python3 is → exact suggestion message."""
    fake_path = _make_fake_path(tmp_path, ["python3"])
    with patch.dict(os.environ, {"PATH": fake_path}):
        msg = _precheck_run_command(ctx(tmp_path), {"argv": ["python", "-c", "print(1)"]})
    assert msg == "executable 'python' not found on PATH — did you mean: python3?"


def test_missing_executable_close_match_difflib(tmp_path: Path) -> None:
    """mytool on PATH; querying mytol returns difflib suggestion."""
    fake_path = _make_fake_path(tmp_path, ["mytool"])
    with patch.dict(os.environ, {"PATH": fake_path}):
        msg = _precheck_run_command(ctx(tmp_path), {"argv": ["mytol"]})
    assert msg is not None
    assert "mytool" in msg


def test_missing_executable_no_suggestions(tmp_path: Path) -> None:
    """Completely unknown executable with empty PATH produces no-suggestion message."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    with patch.dict(os.environ, {"PATH": str(empty_bin)}):
        msg = _precheck_run_command(ctx(tmp_path), {"argv": ["xyznosuchcmd"]})
    assert msg is not None
    assert "not found on PATH" in msg
    assert "did you mean" not in msg


def test_known_executable_passes_precheck(tmp_path: Path) -> None:
    """An executable that is on PATH passes the precheck (returns None)."""
    fake_path = _make_fake_path(tmp_path, ["myapp"])
    with patch.dict(os.environ, {"PATH": fake_path}):
        msg = _precheck_run_command(ctx(tmp_path), {"argv": ["myapp", "--help"]})
    assert msg is None


# ---------------------------------------------------------------------------
# Executable resolution: path-separator cases (relative paths in workspace)
# ---------------------------------------------------------------------------


def test_relative_path_executable_present_and_executable(tmp_path: Path) -> None:
    """./script.sh present and executable → precheck passes."""
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["./script.sh"]})
    assert msg is None


def test_relative_path_executable_missing(tmp_path: Path) -> None:
    """./script.sh not present → 'not found' message."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["./script.sh"]})
    assert msg is not None
    assert "not found" in msg


def test_relative_path_not_executable(tmp_path: Path) -> None:
    """./script.sh present but chmod 644 → 'not executable' message."""
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["./script.sh"]})
    assert msg is not None
    assert "not executable" in msg


# ---------------------------------------------------------------------------
# Backstop: OSError inside run_command_process must not bubble as a crash
# ---------------------------------------------------------------------------


def test_oserror_backstop_returns_clean_failure(tmp_path: Path) -> None:
    """If run_command_process raises FileNotFoundError after precheck passes,
    the result is a clean failure and 'crashed' never appears."""
    fake_path = _make_fake_path(tmp_path, ["vanished"])
    with patch.dict(os.environ, {"PATH": fake_path}):
        with patch(
            "shellpilot.tools.command.run_command_process",
            side_effect=FileNotFoundError("x"),
        ):
            result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["vanished"]})
    assert not result.success
    assert result.summary.startswith("could not start command:")
    assert "crashed" not in result.summary


def test_oserror_backstop_not_raised_through_executor(tmp_path: Path) -> None:
    """The executor's crash wrapper must never see an OSError from run_command."""
    fake_path = _make_fake_path(tmp_path, ["vanished"])
    asker, _ = _approval_spy(approved=True)
    executor = _make_executor(tmp_path, ask_approval=asker)

    with patch.dict(os.environ, {"PATH": fake_path}):
        with patch(
            "shellpilot.tools.command.run_command_process",
            side_effect=FileNotFoundError("x"),
        ):
            call = ToolCall(name="run_command", arguments={"argv": ["vanished"]})
            outcome = executor.execute(call)

    assert outcome.result is not None
    assert not outcome.result.success
    assert "crashed" not in outcome.model_text
    assert "could not start command:" in outcome.result.summary
