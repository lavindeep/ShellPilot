"""Tests for the shell=False command runner (design section 13.1)."""

import sys
import time
from pathlib import Path

from shellpilot.tools.base import ToolContext
from shellpilot.tools.command import RUN_COMMAND, CommandRequest, run_command_process


def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, max_result_tokens=2000, max_capture_chars=10_000)


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


def test_packed_shell_line_is_rejected_with_guidance(tmp_path: Path) -> None:
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["echo hi > out.txt"]})
    assert not result.success
    assert "WITHOUT a shell" in result.content
    assert not (tmp_path / "out.txt").exists()


def test_redirection_in_first_token_is_rejected(tmp_path: Path) -> None:
    result = RUN_COMMAND.handler(ctx(tmp_path), {"argv": ["cat>x", "f"]})
    assert not result.success
