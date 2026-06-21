"""Tests for the themed TerminalUI (design section 31.5/31.6)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

from shellpilot.cli.terminal import (
    TerminalUI,
    _relative_age,
    _resolve_project_agents_trust,
    high_stakes_override_notice,
    should_discard_interrupt,
)
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS
from shellpilot.memory.redaction import REDACTED
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan

GLYPHS = UNICODE_GLYPHS


def make_console() -> Console:
    # no_color=False explicitly overrides the NO_COLOR env var so badge colour
    # assertions are reliable regardless of the test environment.
    return Console(
        record=True,
        width=100,
        file=io.StringIO(),
        theme=SHELLPILOT_THEME,
        force_terminal=True,
        no_color=False,
    )


def make_ui(console: Console, answers: list[str]) -> TerminalUI:
    ui = TerminalUI(console, glyphs=GLYPHS, spinner=False)
    answer_iter: Iterator[str] = iter(answers)

    def fake_input(prompt: str = "", **kwargs: object) -> str:
        console.print(prompt, end="")  # echo like the real Console.input
        return next(answer_iter)

    console.input = fake_input  # type: ignore[method-assign]
    return ui


def medium_request(diff: str = "") -> ApprovalRequest:
    return ApprovalRequest(
        kind="tool",
        display="patch_file hello.py",
        risk=RiskLevel.MEDIUM,
        reasons=("writes inside workspace",),
        cwd=Path("/tmp/ws"),
        diff=diff,
    )


def high_request() -> ApprovalRequest:
    return ApprovalRequest(
        kind="command",
        display="rm -rf build/",
        risk=RiskLevel.HIGH,
        reasons=("recursive delete",),
        cwd=Path("/tmp/ws"),
        purpose="Removes stale build output.",
    )


def test_medium_approval_accepts_yes_case_insensitively() -> None:
    console = make_console()
    assert make_ui(console, ["Y"]).ask_approval(medium_request()) is True
    out = console.export_text()
    assert " MEDIUM " in out
    assert "[y/n]" in out
    assert "[y/N]" not in out


def test_medium_approval_enter_defaults_to_no() -> None:
    console = make_console()
    assert make_ui(console, [""]).ask_approval(medium_request()) is False


def test_high_approval_requires_typed_run() -> None:
    console = make_console()
    assert make_ui(console, ["y"]).ask_approval(high_request()) is False
    console2 = make_console()
    assert make_ui(console2, ["run"]).ask_approval(high_request()) is True
    out = console2.export_text()
    assert " HIGH " in out
    assert "Removes stale build output." in out


def test_approval_renders_diff_panel() -> None:
    diff = '--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-print("done")\n+print("Goodbye World")\n'
    console = make_console()
    make_ui(console, ["y"]).ask_approval(medium_request(diff=diff))
    out = console.export_text()
    assert "hello.py" in out
    assert '+ print("Goodbye World")' in out
    assert "╭" in out  # panel, not raw diff text


def plan() -> TaskPlan:
    return TaskPlan(
        task_id="20260611-040000-demo",
        goal="Demo goal",
        user_intent="demo",
        workspace=Path("/tmp/ws"),
        profile="balanced",
        steps=[PlanStep(title="First", status="completed"), PlanStep(title="Second")],
    )


def test_plan_approval_panel_and_choices() -> None:
    console = make_console()
    ui = make_ui(console, ["y"])
    assert ui.ask_plan_approval(plan(), "/tmp/ws/PLAN.md") == ("y", "")
    out = console.export_text()
    assert "Plan · 20260611-040000-demo" in out
    assert "Goal: Demo goal" in out
    assert "/tmp/ws/PLAN.md" in out


def test_plan_approval_edit_collects_revision() -> None:
    console = make_console()
    ui = make_ui(console, ["e", "add a verification step"])
    assert ui.ask_plan_approval(plan(), "p") == ("e", "add a verification step")


def test_show_plan_progress_prints_checklist() -> None:
    console = make_console()
    ui = make_ui(console, [])
    ui.show_plan_progress(plan())
    out = console.export_text()
    assert f"{GLYPHS.check} 1" in out
    assert f"{GLYPHS.todo} 2" in out


def test_tool_call_and_result_lines() -> None:
    console = make_console()
    ui = make_ui(console, [])
    ui.show_tool_call("patch_file", {"path": "hello.py"})
    ui.show_tool_result("patch_file", True, "1 addition")
    out = console.export_text()
    assert f"{GLYPHS.bullet} patch_file" in out
    assert f"{GLYPHS.check} 1 addition" in out


@pytest.mark.parametrize("method_name", ["show_status", "show_error", "show_command_output"])
def test_terminal_text_sinks_sanitize_external_text(method_name: str) -> None:
    console = make_console()
    ui = make_ui(console, [])

    getattr(ui, method_name)("visible\x1b[2J\x00\ttext\x07\x7f")

    out = console.export_text()
    assert not any(char in out for char in "\x1b\x00\x07\x7f\t")
    assert "visible" in out and "text" in out


# ---------------------------------------------------------------------------
# Fix 2: show_tool_call redacts secrets in the summary display line
# ---------------------------------------------------------------------------


def test_show_tool_call_redacts_secret_in_summary() -> None:
    """show_tool_call must not print raw secrets in the summary line."""
    console = make_console()
    ui = make_ui(console, [])
    # ghp_ token matches the GitHub classic token pattern in redaction.py
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    ui.show_tool_call("run_command", {"token": secret})
    out = console.export_text()
    assert secret not in out
    assert REDACTED in out


def test_show_tool_call_plain_argument_unchanged() -> None:
    """A non-secret argument must render unchanged."""
    console = make_console()
    ui = make_ui(console, [])
    ui.show_tool_call("read_file", {"path": "/tmp/notes.txt"})
    out = console.export_text()
    assert "/tmp/notes.txt" in out


# ---------------------------------------------------------------------------
# A5: show_plan_progress ends with a blank line
# ---------------------------------------------------------------------------


def test_show_plan_progress_ends_with_blank_line() -> None:
    """show_plan_progress must append a blank line so the checklist is visually
    separated from the streamed response that follows."""
    console = make_console()
    ui = make_ui(console, [])
    ui.show_plan_progress(plan())
    raw = console.export_text(clear=False)
    # The exported text should end with two newlines (last content line + blank)
    assert raw.endswith("\n\n"), repr(raw[-20:])


# ---------------------------------------------------------------------------
# A10: per-tool spinner label tests
# ---------------------------------------------------------------------------


class _RecordingSpinner:
    """Minimal spinner double that records start/stop calls."""

    def __init__(self) -> None:
        self.started_labels: list[str | None] = []
        self.stops: int = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, label: object = None) -> None:
        self._active = True
        self.started_labels.append(str(label) if label is not None else None)

    def stop(self) -> None:
        self._active = False
        self.stops += 1


def _ui_with_recording_spinner(
    console: Console, answers: list[str]
) -> tuple[TerminalUI, _RecordingSpinner]:
    """Return a TerminalUI whose spinner is replaced by a _RecordingSpinner."""
    ui = make_ui(console, answers)
    spy = _RecordingSpinner()
    ui._spinner = spy  # type: ignore[assignment]
    return ui, spy


def test_tool_call_starts_labeled_spinner_and_result_stops_it() -> None:
    """show_tool_call starts the spinner with a label; show_tool_result stops it."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])

    ui.show_tool_call("patch_file", {"path": "hello.py"})
    assert len(spy.started_labels) == 1
    label = spy.started_labels[0]
    assert label is not None
    assert "patch_file" in label

    ui.show_tool_result("patch_file", True, "ok")
    assert spy.stops >= 1


def test_tool_call_sanitizes_spinner_label() -> None:
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])

    ui.show_tool_call("visible\x1b[2J\x00text\x07", {})

    assert spy.started_labels == ["running visible[2Jtext"]


def test_approval_stops_spinner_before_input() -> None:
    """ask_approval stops the spinner before prompting the user."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, ["y"])
    spy._active = True  # pretend the spinner is running

    ui.ask_approval(medium_request())
    assert spy.stops >= 1


def test_show_error_stops_spinner() -> None:
    """show_error stops the spinner before printing."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_error("something went wrong")
    assert spy.stops >= 1


def test_show_command_output_stops_spinner() -> None:
    """show_command_output stops the spinner before printing output."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_command_output("line of output")
    assert spy.stops >= 1


def test_show_plan_progress_stops_spinner() -> None:
    """show_plan_progress stops the spinner before printing the checklist."""
    console = make_console()
    ui, spy = _ui_with_recording_spinner(console, [])
    spy._active = True

    ui.show_plan_progress(plan())
    assert spy.stops >= 1


# ---------------------------------------------------------------------------
# Fix 1: should_discard_interrupt timing window
# ---------------------------------------------------------------------------


def test_discard_stale_interrupt_within_window() -> None:
    """A Ctrl-C arriving 0.01 s after a turn just ran should be discarded."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.01) is True


def test_do_not_discard_genuine_late_interrupt() -> None:
    """A Ctrl-C arriving 0.5 s after a turn should NOT be discarded."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.5) is False


def test_do_not_discard_interrupt_at_idle_prompt() -> None:
    """A Ctrl-C at the prompt with no recent turn should NOT be discarded."""
    assert should_discard_interrupt(turn_just_ran=False, elapsed_seconds=0.01) is False


def test_discard_at_exact_boundary_is_false() -> None:
    """The boundary (elapsed == window) is exclusive: do NOT discard at exactly window_seconds."""
    assert should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.1) is False


def test_custom_window_seconds() -> None:
    """The window_seconds parameter is respected."""
    assert (
        should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.04, window_seconds=0.05)
        is True
    )
    assert (
        should_discard_interrupt(turn_just_ran=True, elapsed_seconds=0.06, window_seconds=0.05)
        is False
    )


def make_trust_console(answers: list[str]) -> Console:
    console = make_console()
    answer_iter: Iterator[str] = iter(answers)

    def fake_input(prompt: str = "", **kwargs: object) -> str:
        console.print(prompt, end="")
        return next(answer_iter)

    console.input = fake_input  # type: ignore[method-assign]
    return console


def test_trust_no_project_agents_md_returns_true(tmp_path: Path) -> None:
    console = make_console()
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is True


def test_trust_already_trusted_digest_no_prompt(tmp_path: Path) -> None:
    from shellpilot.memory.agents_md import project_agents_md_digest
    from shellpilot.persistence.workspace_state import save_trusted_agents_digest

    (tmp_path / "AGENTS.md").write_text("Project rules.", encoding="utf-8")
    digest = project_agents_md_digest(tmp_path)
    assert digest is not None
    save_trusted_agents_digest(tmp_path, digest)
    # No input wired: if it tried to prompt, this would raise StopIteration.
    console = make_trust_console([])
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is True


def test_trust_non_tty_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Project rules.", encoding="utf-8")
    console = make_console()
    assert _resolve_project_agents_trust(console, tmp_path, tty=False) is False
    assert "not loaded" in console.export_text()


def test_trust_accept_records_digest(tmp_path: Path) -> None:
    from shellpilot.memory.agents_md import project_agents_md_digest
    from shellpilot.persistence.workspace_state import load_trusted_agents_digest

    (tmp_path / "AGENTS.md").write_text("Project rules.", encoding="utf-8")
    console = make_trust_console(["y"])
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is True
    assert load_trusted_agents_digest(tmp_path) == project_agents_md_digest(tmp_path)


def test_trust_decline_does_not_record(tmp_path: Path) -> None:
    from shellpilot.persistence.workspace_state import load_trusted_agents_digest

    (tmp_path / "AGENTS.md").write_text("Project rules.", encoding="utf-8")
    console = make_trust_console(["n"])
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is False
    assert load_trusted_agents_digest(tmp_path) is None


def test_trust_changed_content_reprompts(tmp_path: Path) -> None:
    from shellpilot.memory.agents_md import project_agents_md_digest
    from shellpilot.persistence.workspace_state import save_trusted_agents_digest

    agents = tmp_path / "AGENTS.md"
    agents.write_text("Original rules.", encoding="utf-8")
    save_trusted_agents_digest(tmp_path, project_agents_md_digest(tmp_path) or "")
    agents.write_text("Tampered rules.", encoding="utf-8")
    console = make_trust_console(["n"])
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is False
    assert "changed since" in console.export_text()


def test_trust_eof_declines(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Project rules.", encoding="utf-8")
    console = make_console()

    def raise_eof(prompt: str = "", **kwargs: object) -> str:
        raise EOFError

    console.input = raise_eof  # type: ignore[method-assign]
    assert _resolve_project_agents_trust(console, tmp_path, tty=True) is False


# ---------------------------------------------------------------------------
# Cloud-egress consent gate (v0.10.0 Part 2): per-session y/N, fail-closed.
# ---------------------------------------------------------------------------


def _settings(*, allow_cloud: bool = False, base_url: str = "http://localhost:11434"):
    from shellpilot.config.model import ModelSettings, Settings

    return Settings(model=ModelSettings(allow_cloud=allow_cloud, base_url=base_url))


def test_consent_local_model_no_prompt(tmp_path: Path) -> None:
    """A local model on localhost is non-egressing → proceed, NO prompt shown."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    # No input wired: a prompt would raise StopIteration.
    console = make_trust_console([])
    assert _resolve_cloud_consent(console, _settings(), "gemma4:e4b", tty=True) is True
    assert console.export_text().strip() == ""


def test_consent_cloud_model_allow_off_rejects(tmp_path: Path) -> None:
    """A cloud model with allow_cloud off is refused with a clear message — no prompt."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_trust_console([])
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=False), "nemotron-3-nano:30b-cloud", tty=True
        )
        is False
    )
    out = console.export_text()
    assert "allow_cloud" in out


def test_consent_cloud_model_non_tty_fails_closed(tmp_path: Path) -> None:
    """allow_cloud on but non-interactive → fail closed (no egress without consent)."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_trust_console([])
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=True), "nemotron-3-nano:30b-cloud", tty=False
        )
        is False
    )
    assert "non-interactive" in console.export_text()


def test_consent_cloud_model_accepts_yes(tmp_path: Path) -> None:
    """allow_cloud on + tty + 'y' → proceed; the disclosure text is shown."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_trust_console(["y"])
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=True), "nemotron-3-nano:30b-cloud", tty=True
        )
        is True
    )
    out = console.export_text()
    # The disclosure must be honest about what leaves the device.
    assert "remote" in out.lower() or "leaves" in out.lower()


def test_consent_cloud_model_enter_declines(tmp_path: Path) -> None:
    """Default No: a bare Enter declines (fail closed)."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_trust_console([""])
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=True), "nemotron-3-nano:30b-cloud", tty=True
        )
        is False
    )


def test_consent_cloud_model_explicit_no_declines(tmp_path: Path) -> None:
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_trust_console(["n"])
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=True), "nemotron-3-nano:30b-cloud", tty=True
        )
        is False
    )


def test_consent_cloud_model_eof_declines(tmp_path: Path) -> None:
    """EOF at the consent prompt fails closed."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    console = make_console()

    def raise_eof(prompt: str = "", **kwargs: object) -> str:
        raise EOFError

    console.input = raise_eof  # type: ignore[method-assign]
    assert (
        _resolve_cloud_consent(
            console, _settings(allow_cloud=True), "nemotron-3-nano:30b-cloud", tty=True
        )
        is False
    )


def test_consent_remote_base_url_local_model_requires_consent(tmp_path: Path) -> None:
    """A non-loopback base_url egresses even for a local-looking model name."""
    from shellpilot.cli.terminal import _resolve_cloud_consent

    settings = _settings(allow_cloud=True, base_url="https://ollama.com")
    console = make_trust_console([""])
    assert _resolve_cloud_consent(console, settings, "gemma4:e4b", tty=True) is False
    # And with allow_cloud off it is refused before any prompt.
    settings_off = _settings(allow_cloud=False, base_url="https://ollama.com")
    console2 = make_trust_console([])
    assert _resolve_cloud_consent(console2, settings_off, "gemma4:e4b", tty=True) is False
    assert "allow_cloud" in console2.export_text()


# ---------------------------------------------------------------------------
# P4-B review nit #9: the streamlined one-key cloud-confirm path MUST reach the
# cloud consent gate. There is no other end-to-end guard that the picker's
# Enter-to-fly-the-last-model shortcut routes a cloud model through
# _resolve_cloud_consent before any model-touching call — a regression a future
# picker refactor could silently reintroduce. This drives run_interactive
# through that exact path: a cloud last_model, confirmed via the one-key Enter
# shortcut, with consent stubbed to refuse, and asserts (a) consent WAS invoked
# for the chosen cloud model and (b) the run aborts BEFORE client.preload (the
# first egress point), so nothing touched the model.
# ---------------------------------------------------------------------------


def test_one_key_cloud_confirm_reaches_consent_gate_before_preload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shellpilot.cli.terminal as terminal_mod
    from shellpilot.llm.ollama import LocalModel

    cloud_model = "nemotron-3-nano:30b-cloud"

    class _FakeClient:
        """Minimal OllamaClient double for the boot-path picker/consent seam."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.preload_calls: list[str] = []

        def health(self) -> bool:
            return True

        def list_models(self) -> list[LocalModel]:
            # The cloud model appears in /api/tags here so confirm_last_model's
            # Enter-to-fly path selects it without opening the menu.
            return [LocalModel(name=cloud_model, size_bytes=1)]

        def preload(self, model: str, *, keep_alive: str = "5m") -> None:
            self.preload_calls.append(model)

    fake_client = _FakeClient()
    monkeypatch.setattr(terminal_mod, "OllamaClient", lambda *a, **k: fake_client)

    # Force the picker to show and take the one-key Enter ("fly the last model")
    # shortcut for the cloud last_model — no menu, single confirm keystroke.
    monkeypatch.setattr(terminal_mod, "should_show_picker", lambda **kwargs: True)
    monkeypatch.setattr(terminal_mod, "load_last_model", lambda workspace: cloud_model)
    monkeypatch.setattr(terminal_mod, "save_last_model", lambda workspace, chosen: None)
    monkeypatch.setattr(terminal_mod, "confirm_last_model", lambda console, last: True)

    def _fail_choose_model(*args: object, **kwargs: object) -> str:
        raise AssertionError("the full menu must not open on the one-key Enter path")

    monkeypatch.setattr(terminal_mod, "choose_model", _fail_choose_model)

    # Stub the consent gate to record the model it was asked about and refuse,
    # so the boot must abort at the consent boundary.
    consent_calls: list[str] = []

    def _record_consent(console: object, settings: object, chosen: str, *, tty: bool) -> bool:
        consent_calls.append(chosen)
        return False

    monkeypatch.setattr(terminal_mod, "_resolve_cloud_consent", _record_consent)

    # Make the boot path believe it is an interactive TTY.
    monkeypatch.setattr(terminal_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Console, "is_terminal", property(lambda self: True))

    rc = terminal_mod.run_interactive(tmp_path)

    assert consent_calls == [cloud_model], "consent gate not reached for the chosen cloud model"
    assert fake_client.preload_calls == [], "the model was touched despite refused consent"
    assert rc == 1, "a refused cloud consent must abort the boot"


def test_high_stakes_override_notice_lists_active_egress_overrides(tmp_path: Path) -> None:
    """A persisted high-stakes override surfaces an amber notice every boot."""
    from shellpilot.config.overrides import overrides_path, save_overrides

    save_overrides(
        overrides_path(tmp_path),
        {"model.allow_cloud": True, "tools.web": True, "runtime.max_tool_turns": 20},
    )
    notice = high_stakes_override_notice(tmp_path)
    assert notice is not None
    assert "model.allow_cloud=True" in notice
    assert "tools.web=True" in notice
    # Non-high-stakes overrides are not surfaced by this notice.
    assert "max_tool_turns" not in notice
    assert "/config unset" in notice


def test_high_stakes_override_notice_none_when_clean(tmp_path: Path) -> None:
    """No high-stakes override → no notice (default boot prints nothing)."""
    from shellpilot.config.overrides import overrides_path, save_overrides

    # No overrides file at all.
    assert high_stakes_override_notice(tmp_path) is None
    # An overrides file with only ordinary keys also yields no notice.
    save_overrides(overrides_path(tmp_path), {"runtime.max_tool_turns": 20})
    assert high_stakes_override_notice(tmp_path) is None


def test_relative_age_buckets() -> None:
    now = 1_000_000.0
    assert _relative_age(now - 10, now=now) == "just now"
    assert _relative_age(now - 39 * 60, now=now) == "39m ago"
    assert _relative_age(now - 2 * 3600, now=now) == "2h ago"
    assert _relative_age(now - 3 * 86400, now=now) == "3d ago"
    # Future / clock skew never goes negative.
    assert _relative_age(now + 100, now=now) == "just now"


def test_bang_prefix_runs_manual_shell_not_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`!<cmd>` routes to the audited manual-shell path and is never sent to the model."""
    import shellpilot.cli.terminal as terminal_mod
    from shellpilot.llm.ollama import LocalModel
    from shellpilot.persistence.paths import AppPaths

    model = "gemma4:e4b"

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def health(self) -> bool:
            return True

        def list_models(self) -> list[LocalModel]:
            return [LocalModel(name=model, size_bytes=1)]

        def preload(self, name: str, *, keep_alive: str = "5m") -> None:
            pass

        def model_context_length(self, name: str) -> int:
            return 8192

    monkeypatch.setattr(terminal_mod, "OllamaClient", lambda *a, **k: _FakeClient())
    # Redirect app dirs to tmp so boot-time audit/session/memory never touch real state.
    fake_paths = AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(terminal_mod.AppPaths, "default", classmethod(lambda cls: fake_paths))

    # Feed one `!` line, then EOF to end the REPL.
    class _Reader:
        def __init__(self) -> None:
            self._lines = iter(["!echo hi"])

        def read(self, context: object) -> str:
            try:
                return next(self._lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(terminal_mod, "make_input", lambda *a, **k: _Reader())

    bang_calls: list[str] = []
    monkeypatch.setattr(
        terminal_mod,
        "run_manual_command",
        lambda command, cwd, audit: bang_calls.append(command) or 0,
    )
    model_turns: list[str] = []
    monkeypatch.setattr(
        terminal_mod.ConversationRuntime,
        "run_turn",
        lambda self, text, **k: model_turns.append(text) or "",
    )
    monkeypatch.setattr(terminal_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Console, "is_terminal", property(lambda self: True))

    rc = terminal_mod.run_interactive(tmp_path, model_override=model)

    assert bang_calls == ["echo hi"], "'!<cmd>' must run via the manual-shell path"
    assert model_turns == [], "a '!' line must NOT be sent to the model"
    assert rc == 0
