"""Tests for the shell=False command runner (design section 13.1)."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from shellpilot.llm.messages import ToolCall
from shellpilot.policy.approvals import APPROVE, DECLINE, ApprovalReply
from shellpilot.runtime.executor import ToolExecutor
from shellpilot.tools.base import ToolContext
from shellpilot.tools.command import (
    MAX_READ_CHARS,
    RUN_COMMAND,
    CommandRequest,
    _precheck_run_command,
    run_command_process,
    scrub_own_environment,
    subprocess_env,
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

    def _ask(request: Any) -> ApprovalReply:
        calls.append(request)
        return APPROVE if approved else DECLINE

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


def test_cancel_kills_process_group_fast(tmp_path: Path) -> None:
    # Branch 6b (§31.15): a cancel event set mid-command kills the child's whole
    # process group at once, instead of waiting out the (here, 30 s) timeout.
    cancel = threading.Event()
    captured: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def _capture(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        proc = real_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    baseline_threads = threading.active_count()
    setter = threading.Thread(target=lambda: (time.sleep(0.2), cancel.set()))  # type: ignore[func-returns-value]
    start = time.monotonic()
    setter.start()
    with patch("shellpilot.tools.command.subprocess.Popen", side_effect=_capture):
        outcome = run_command_process(
            CommandRequest(
                argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                timeout_seconds=30,
            ),
            max_capture_chars=1000,
            cancel=cancel,
        )
    elapsed = time.monotonic() - start
    setter.join()

    # The cancel-kill is proven by the fast return (not the 30 s sleep) + the dead
    # process group below, not by a flag: the turn abort is driven by the cancel
    # Event in the tool loop, so the command outcome carries no "cancelled" flag.
    assert outcome.timed_out is False
    assert elapsed < 8  # returned promptly, not after the 30 s sleep

    # The child's process group was SIGKILLed: start_new_session=True makes the
    # child its own group leader (pgid == pid), so signalling it now raises.
    pgid = captured[0].pid
    try:
        os.killpg(pgid, 0)
        raise AssertionError("process group still alive after cancel")
    except ProcessLookupError:
        pass

    # The reader thread was joined — no leak (active count back to baseline).
    assert threading.active_count() == baseline_threads


def test_cancel_none_completes_normally(tmp_path: Path) -> None:
    # The legacy path (cancel=None, e.g. the default REPL executor) is unchanged:
    # a fast command exits normally.
    outcome = run_command_process(
        CommandRequest(argv=["echo", "hi"], cwd=tmp_path, timeout_seconds=30),
        max_capture_chars=1000,
        cancel=None,
    )
    assert outcome.timed_out is False
    assert outcome.exit_code == 0
    assert "hi" in outcome.output


def test_output_capture_is_bounded(tmp_path: Path) -> None:
    # A single newline-less chunk far larger than the cap must be hard-bounded to
    # exactly max_capture_chars with truncated=True — not appended whole (which
    # overshot the cap by up to one chunk and left truncated=False).
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "print('x' * 1000, end='')"],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=50,
    )
    assert len(outcome.output) == 50
    assert outcome.truncated


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


# ---------------------------------------------------------------------------
# Shell-operator tokens anywhere in argv (A-5 Gap 1)
# ---------------------------------------------------------------------------


def test_pipe_token_in_argv_is_rejected(tmp_path: Path) -> None:
    """argv containing a standalone | token is rejected pre-approval."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["ls", "-la", "|", "grep", "py"]})
    assert msg is not None
    assert "'|'" in msg


def test_pipe_token_pre_approval_no_ask(tmp_path: Path) -> None:
    """No approval request is made when argv contains a pipe token."""
    asker, calls = _approval_spy()
    executor = _make_executor(tmp_path, ask_approval=asker)
    call = ToolCall(name="run_command", arguments={"argv": ["ls", "-la", "|", "grep", "py"]})
    outcome = executor.execute(call)
    assert not outcome.result.success  # type: ignore[union-attr]
    assert len(calls) == 0, "approval must not be requested for shell-operator argv"


def test_double_ampersand_token_is_rejected(tmp_path: Path) -> None:
    """Standalone && token is rejected."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["true", "&&", "true"]})
    assert msg is not None
    assert "'&&'" in msg


def test_redirect_out_token_is_rejected(tmp_path: Path) -> None:
    """Standalone > token is rejected."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["echo", ">", "out.txt"]})
    assert msg is not None
    assert "'>'" in msg


def test_find_exec_semicolon_not_rejected(tmp_path: Path) -> None:
    """find -exec ... ; passes a literal ; token — must NOT be rejected by operator check."""
    # find may or may not be on PATH; we only care that operator check doesn't fire.
    msg = _precheck_run_command(
        ctx(tmp_path),
        {"argv": ["find", ".", "-name", "*.py", "-exec", "head", "-1", "{}", ";"]},
    )
    # If find is not on PATH the msg will mention "not found", not "shell operator"
    if msg is not None:
        assert "shell operator" not in msg


def test_regex_pipe_in_arg_not_rejected(tmp_path: Path) -> None:
    """A token that merely contains | (e.g. a regex) is NOT rejected."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["grep", "-E", "a|b", "file.txt"]})
    # grep is on PATH on any standard system; if not, the miss message won't mention operator
    if msg is not None:
        assert "shell operator" not in msg


# ---------------------------------------------------------------------------
# Did-you-mean suggestion for packed single-token lines (A-5 Gap 2)
# ---------------------------------------------------------------------------


def test_packed_clean_token_suggests_split_argv(tmp_path: Path) -> None:
    """Packed token with no shell syntax gets a Did you mean argv=[...] suggestion."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["python -m unittest test_calculator.py"]})
    assert msg is not None
    assert 'Did you mean argv=["python", "-m", "unittest", "test_calculator.py"]?' in msg


def test_packed_with_shell_syntax_no_suggestion(tmp_path: Path) -> None:
    """Packed token containing shell syntax does NOT get a Did you mean suggestion."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["echo hi > f.txt"]})
    assert msg is not None
    assert "Did you mean" not in msg


def test_packed_unbalanced_quote_no_crash_no_suggestion(tmp_path: Path) -> None:
    """Packed token with unbalanced quote raises no crash and produces no suggestion."""
    msg = _precheck_run_command(ctx(tmp_path), {"argv": ["echo 'oops"]})
    assert msg is not None
    assert "Did you mean" not in msg


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


# ---------------------------------------------------------------------------
# subprocess_env: sanitized, non-interactive environment (section 13.1)
# ---------------------------------------------------------------------------


def test_scrub_own_environment_removes_debug_vars() -> None:
    """Boot scrub deletes DYLD_/LD_/Malloc vars from this process, keeps others."""
    with patch.dict(
        os.environ,
        {
            "DYLD_SCRUB_TEST": "1",
            "MallocScrubTest": "1",
            "LD_SCRUB_TEST": "1",
            "SHELLPILOT_SCRUB_KEEP": "1",
        },
    ):
        scrub_own_environment()
        assert "DYLD_SCRUB_TEST" not in os.environ
        assert "MallocScrubTest" not in os.environ
        assert "LD_SCRUB_TEST" not in os.environ
        assert os.environ["SHELLPILOT_SCRUB_KEEP"] == "1"


def test_subprocess_env_strips_debug_vars(tmp_path: Path) -> None:
    """DYLD_*, Malloc*, and LD_* vars injected into parent env are absent in child."""
    with patch.dict(
        os.environ,
        {"DYLD_FAKE_TEST": "1", "Malloc_FAKE_TEST": "1", "LD_FAKE_TEST": "1"},
    ):
        env = subprocess_env()
        outcome = run_command_process(
            CommandRequest(
                argv=[
                    sys.executable,
                    "-c",
                    "import os; print(','.join("
                    "k for k in os.environ"
                    " if k.startswith(('DYLD_', 'Malloc', 'LD_'))))",
                ],
                cwd=tmp_path,
                timeout_seconds=30,
                env=env,
            ),
            max_capture_chars=10_000,
        )
    assert outcome.exit_code == 0
    assert outcome.output.strip() == ""


def test_subprocess_env_forces_non_interactive_vars(tmp_path: Path) -> None:
    """GIT_PAGER and GIT_TERMINAL_PROMPT are set to non-interactive values."""
    outcome = run_command_process(
        CommandRequest(
            argv=[
                sys.executable,
                "-c",
                "import os; print(os.environ['GIT_PAGER'], os.environ['GIT_TERMINAL_PROMPT'])",
            ],
            cwd=tmp_path,
            timeout_seconds=30,
            env=subprocess_env(),
        ),
        max_capture_chars=10_000,
    )
    assert outcome.exit_code == 0
    pager, prompt = outcome.output.strip().split()
    assert pager == "cat"
    assert prompt == "0"


def test_subprocess_env_stdin_eof(tmp_path: Path) -> None:
    """Child that reads stdin gets immediate EOF; completes well within timeout."""
    start = time.monotonic()
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
            cwd=tmp_path,
            timeout_seconds=10,
            env=subprocess_env(),
        ),
        max_capture_chars=10_000,
    )
    elapsed = time.monotonic() - start
    assert outcome.exit_code == 0
    assert outcome.output.strip() == "0"
    assert elapsed < 5  # well under the 10 s timeout


def test_subprocess_env_path_preserved() -> None:
    """PATH from the parent environment passes through subprocess_env() unchanged."""
    assert subprocess_env()["PATH"] == os.environ["PATH"]


def test_subprocess_env_override_not_forced_in_popen(tmp_path: Path) -> None:
    """CommandRequest.env is passed to Popen as-is; subprocess_env() is not forced."""
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "import os; print(os.environ.get('ONLY', 'missing'))"],
            cwd=tmp_path,
            timeout_seconds=10,
            env={"ONLY": "this", "PATH": os.environ.get("PATH", "")},
        ),
        max_capture_chars=10_000,
    )
    assert outcome.exit_code == 0
    assert outcome.output.strip() == "this"


# ---------------------------------------------------------------------------
# Timeout clamping: model-supplied timeout_seconds is bounded by the ceiling
# from ToolContext.command_timeout_seconds (design section 13.1)
# ---------------------------------------------------------------------------


def _make_executor_with_timeout(tmp_path: Path, ceiling: int) -> ToolExecutor:
    """Build an executor with RUN_COMMAND and a specific command_timeout_seconds."""
    registry = ToolRegistry()
    registry.register(RUN_COMMAND)
    return ToolExecutor(
        registry=registry,
        workspace=tmp_path,
        profile="supervised",
        max_result_tokens=2000,
        max_total_tokens=10_000,
        command_timeout_seconds=ceiling,
        ask_approval=lambda req: APPROVE,
    )


def test_timeout_clamped_to_ceiling_when_model_exceeds_it(tmp_path: Path) -> None:
    """Model asks 600 but ceiling is 5 — request must use 5."""
    captured: list[Any] = []
    with patch(
        "shellpilot.tools.command.run_command_process",
        side_effect=lambda req, **kw: captured.append(req) or _fake_outcome(),
    ):
        context = ToolContext(
            workspace=tmp_path,
            max_result_tokens=2000,
            max_capture_chars=10_000,
            command_timeout_seconds=5,
        )
        RUN_COMMAND.handler(context, {"argv": ["echo", "hi"], "timeout_seconds": 600})
    assert len(captured) == 1
    assert captured[0].timeout_seconds == 5


def test_timeout_shorter_than_ceiling_is_respected(tmp_path: Path) -> None:
    """Model asks 2 under a ceiling of 600 — request must use 2."""
    captured: list[Any] = []
    with patch(
        "shellpilot.tools.command.run_command_process",
        side_effect=lambda req, **kw: captured.append(req) or _fake_outcome(),
    ):
        context = ToolContext(
            workspace=tmp_path,
            max_result_tokens=2000,
            max_capture_chars=10_000,
            command_timeout_seconds=600,
        )
        RUN_COMMAND.handler(context, {"argv": ["echo", "hi"], "timeout_seconds": 2})
    assert len(captured) == 1
    assert captured[0].timeout_seconds == 2


def test_timeout_zero_is_floored_to_one(tmp_path: Path) -> None:
    """Model asks 0 — floor clamps to 1."""
    captured: list[Any] = []
    with patch(
        "shellpilot.tools.command.run_command_process",
        side_effect=lambda req, **kw: captured.append(req) or _fake_outcome(),
    ):
        context = ToolContext(
            workspace=tmp_path,
            max_result_tokens=2000,
            max_capture_chars=10_000,
            command_timeout_seconds=600,
        )
        RUN_COMMAND.handler(context, {"argv": ["echo", "hi"], "timeout_seconds": 0})
    assert len(captured) == 1
    assert captured[0].timeout_seconds == 1


def test_timeout_defaults_to_ceiling_when_omitted(tmp_path: Path) -> None:
    """No timeout_seconds argument — default is the ceiling value."""
    captured: list[Any] = []
    with patch(
        "shellpilot.tools.command.run_command_process",
        side_effect=lambda req, **kw: captured.append(req) or _fake_outcome(),
    ):
        context = ToolContext(
            workspace=tmp_path,
            max_result_tokens=2000,
            max_capture_chars=10_000,
            command_timeout_seconds=300,
        )
        RUN_COMMAND.handler(context, {"argv": ["echo", "hi"]})
    assert len(captured) == 1
    assert captured[0].timeout_seconds == 300


def test_timeout_default_flow_unchanged_at_600(tmp_path: Path) -> None:
    """Default ToolContext (ceiling=600) with no model argument uses 600."""
    captured: list[Any] = []
    with patch(
        "shellpilot.tools.command.run_command_process",
        side_effect=lambda req, **kw: captured.append(req) or _fake_outcome(),
    ):
        context = ToolContext(
            workspace=tmp_path,
            max_result_tokens=2000,
            max_capture_chars=10_000,
        )
        RUN_COMMAND.handler(context, {"argv": ["echo", "hi"]})
    assert len(captured) == 1
    assert captured[0].timeout_seconds == 600


def _fake_outcome() -> Any:
    """Minimal CommandOutcome substitute for monkeypatching run_command_process."""
    from shellpilot.tools.command import CommandOutcome

    return CommandOutcome(exit_code=0, output="", timed_out=False, truncated=False)


# ---------------------------------------------------------------------------
# Newline-less output: per-read chunk bound (#17)
# ---------------------------------------------------------------------------


def test_newline_less_runaway_is_truncated(tmp_path: Path) -> None:
    """A newline-less 5 MB stream is hard-bounded; truncated flag is set.

    Pre-fix: the text-mode line iterator materialises the entire 5 MB string
    before the cap is consulted, so captured output == 5 MB and truncated stays
    False.  Post-fix: readline(MAX_READ_CHARS) caps each read so total capture
    stays within max_capture_chars + MAX_READ_CHARS.
    """
    cap = 200_000
    outcome = run_command_process(
        CommandRequest(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 5_000_000); sys.stdout.flush()",
            ],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=cap,
    )
    assert outcome.truncated is True, "truncated must be set for newline-less runaway"
    assert len(outcome.output) <= cap + MAX_READ_CHARS, (
        f"output length {len(outcome.output)} exceeds cap + MAX_READ_CHARS "
        f"({cap} + {MAX_READ_CHARS} = {cap + MAX_READ_CHARS})"
    )


def test_newline_less_small_output_untruncated(tmp_path: Path) -> None:
    """A short newline-less stream (under the cap) is captured fully, not truncated."""
    outcome = run_command_process(
        CommandRequest(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('hello'); sys.stdout.flush()",
            ],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=200_000,
    )
    assert not outcome.truncated
    assert "hello" in outcome.output


def test_multiline_normal_output_untruncated(tmp_path: Path) -> None:
    """Normal multi-line output well under the cap is captured in full."""
    outcome = run_command_process(
        CommandRequest(
            argv=[sys.executable, "-c", "for i in range(10): print(f'line {i}')"],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=200_000,
    )
    assert not outcome.truncated
    for i in range(10):
        assert f"line {i}" in outcome.output


def test_newline_rich_over_cap_still_truncates(tmp_path: Path) -> None:
    """Newline-rich output that exceeds the cap still sets truncated (regression guard)."""
    cap = 500
    outcome = run_command_process(
        CommandRequest(
            argv=[
                sys.executable,
                "-c",
                "for i in range(200): print('x' * 20)",
            ],
            cwd=tmp_path,
            timeout_seconds=30,
        ),
        max_capture_chars=cap,
    )
    assert outcome.truncated is True
    # Each readline stops at the '\n' (line is 21 chars), so total <= cap + 21.
    assert len(outcome.output) <= cap + 21
