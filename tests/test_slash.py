"""Tests for slash-command dispatch."""

from pathlib import Path

import pytest
from rich.console import Console

from shellpilot.cli.slash import SlashAction, SlashDispatcher
from shellpilot.config.loader import LoadedConfig, load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI


class Harness:
    def __init__(self, tmp_path: Path, confirm_answer: bool = True) -> None:
        self.console = Console(record=True, width=100)
        self.fake = FakeLLM(script=[answer("hello")])
        self.loaded = self._load(tmp_path)
        self.runtime = ConversationRuntime(
            llm=self.fake,
            settings=self.loaded.settings,
            workspace=tmp_path,
            behavior=BehaviorInstructions(global_text=None, project_text=None),
            ui=FakeUI(),
        )
        self.reloads = 0

        def reload_config() -> LoadedConfig:
            self.reloads += 1
            return self._load(tmp_path)

        self.dispatcher = SlashDispatcher(
            runtime=self.runtime,
            client=self.fake,
            console=self.console,
            loaded=self.loaded,
            user_config_file=tmp_path / "config.toml",
            reload_config=reload_config,
            confirm=lambda prompt: confirm_answer,
        )

    @staticmethod
    def _load(tmp_path: Path) -> LoadedConfig:
        return load_config(
            user_config_file=tmp_path / "missing-user.toml",
            project_config_file=tmp_path / "missing-project.toml",
            env={},
        )

    def output(self) -> str:
        return self.console.export_text()


def test_exit_and_quit(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.dispatcher.handle("/exit") is SlashAction.EXIT
    assert harness.dispatcher.handle("/quit") is SlashAction.EXIT


def test_help_lists_commands(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.dispatcher.handle("/help") is SlashAction.CONTINUE
    out = harness.output()
    assert "/status" in out
    assert "/model" in out


def test_unknown_command_reports(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/bogus")
    assert "Unknown command" in harness.output()


def test_clear_requires_confirmation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, confirm_answer=False)
    harness.runtime.run_turn("hi")
    harness.dispatcher.handle("/clear")
    assert harness.runtime.status().history_messages == 2  # declined

    harness2 = Harness(tmp_path, confirm_answer=True)
    harness2.runtime.run_turn("hi")
    harness2.dispatcher.handle("/clear")
    assert harness2.runtime.status().history_messages == 0


def test_status_shows_model_and_context(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/status")
    out = harness.output()
    assert "gemma4:e4b" in out
    assert "balanced" in out
    assert "8192" in out


def test_model_list_shows_all_models_with_tags(tmp_path: Path) -> None:
    """All installed models appear in /model list, each tagged tested or untested."""
    from shellpilot.llm.ollama import LocalModel

    harness = Harness(tmp_path)
    harness.fake.models = [
        LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000),
        LocalModel(name="llama3:8b", size_bytes=4_000_000_000),
    ]
    harness.dispatcher.handle("/model list")
    out = harness.output()
    assert "gemma4:e4b" in out
    assert "llama3:8b" in out
    assert "tested" in out
    assert "untested" in out


def test_model_use_switches_installed_model(tmp_path: Path) -> None:
    from shellpilot.llm.ollama import LocalModel

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000))
    harness.dispatcher.handle("/model use gemma4:e2b")
    assert harness.runtime.model == "gemma4:e2b"


def test_model_use_rejects_missing_model(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/model use nope:1b")
    assert harness.runtime.model == "gemma4:e4b"
    assert "not installed" in harness.output()


def test_model_use_untested_notes_benchmark(tmp_path: Path) -> None:
    """/model use <untested> prints a dim note mentioning benchmark path."""
    from shellpilot.llm.ollama import LocalModel

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="llama3:8b", size_bytes=4_000_000_000))
    harness.dispatcher.handle("/model use llama3:8b")
    out = harness.output()
    assert harness.runtime.model == "llama3:8b"
    assert "not a tested family" in out
    assert "benchmark_model.py" in out


def test_model_use_saves_last_model(tmp_path: Path) -> None:
    """/model use <name> persists the choice via save_last_model."""
    from shellpilot.llm.ollama import LocalModel
    from shellpilot.persistence.workspace_state import load_last_model

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000))
    harness.dispatcher.handle("/model use gemma4:e2b")
    assert load_last_model(tmp_path) == "gemma4:e2b"


def test_model_use_survives_unwritable_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/model use still switches the model if save_last_model raises OSError."""
    import shellpilot.persistence.workspace_state as ws_mod
    from shellpilot.llm.ollama import LocalModel

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000))
    monkeypatch.setattr(
        ws_mod,
        "save_last_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only")),
    )

    harness.dispatcher.handle("/model use gemma4:e2b")

    assert harness.runtime.model == "gemma4:e2b"
    assert "Warning: could not save model choice" in harness.output()


def test_config_show_includes_sources(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config show")
    out = harness.output()
    assert "model.default" in out
    assert "default" in out


def test_config_reload_calls_loader(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config reload")
    assert harness.reloads == 1
    assert "reloaded" in harness.output().lower()


def test_compact_status_shows_thresholds(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/compact status")
    out = harness.output()
    assert "Compact at: 5734" in out
    assert "Hard limit: 7372" in out


def test_context_shows_blocks_and_total(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/context")
    out = harness.output()
    assert "base prompt" in out
    assert "skills index" in out
    assert "tool schemas" in out
    assert "history" in out
    assert "TOTAL" in out


def test_cwd_set_changes_boundary_with_confirmation(tmp_path: Path) -> None:
    new_workspace = tmp_path / "other"
    new_workspace.mkdir()
    harness = Harness(tmp_path, confirm_answer=True)
    harness.dispatcher.handle(f"/cwd set {new_workspace}")
    assert harness.runtime.status().workspace == new_workspace.resolve()


def test_cwd_set_rejects_missing_dir(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle(f"/cwd set {tmp_path / 'nope'}")
    assert harness.runtime.status().workspace == tmp_path
    assert "not a directory" in harness.output()


def test_profile_use_switches_and_audits(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/profile use supervised")
    assert harness.runtime.settings.runtime.security_profile == "supervised"
    harness.dispatcher.handle("/profile use trusted-local")
    assert harness.runtime.settings.runtime.security_profile == "supervised"  # rejected
    assert "not a v1 profile" in harness.output()


def test_compact_auto_toggle(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.runtime.settings.runtime.auto_compact is True
    harness.dispatcher.handle("/compact auto off")
    assert harness.runtime.settings.runtime.auto_compact is False
    harness.dispatcher.handle("/compact status")
    assert "Automatic compaction: off" in harness.output()
    harness.dispatcher.handle("/compact auto on")
    assert harness.runtime.settings.runtime.auto_compact is True


def test_export_without_session_store_reports(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/export")
    assert "No session transcript" in harness.output()


# ---------------------------------------------------------------------------
# A4: /clear confirm copy and post-clear feedback
# ---------------------------------------------------------------------------


def test_clear_confirm_prompt_mentions_plan(tmp_path: Path) -> None:
    """The /clear confirmation prompt mentions plan cancellation."""
    prompts: list[str] = []

    harness = Harness(tmp_path, confirm_answer=False)
    harness.dispatcher._confirm = lambda p: (prompts.append(p), False)[1]  # type: ignore[method-assign]
    harness.dispatcher.handle("/clear")

    assert prompts, "confirm was never called"
    assert "plan" in prompts[0].lower()


# ---------------------------------------------------------------------------
# A10: preload callback in /model use
# ---------------------------------------------------------------------------


def test_model_use_triggers_preload(tmp_path: Path) -> None:
    """/model use <name> calls the preload callable with the new model name."""
    from shellpilot.llm.ollama import LocalModel

    preloaded: list[str] = []

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000))
    harness.dispatcher._preload = lambda name: preloaded.append(name)  # type: ignore[attr-defined]

    harness.dispatcher.handle("/model use gemma4:e2b")

    assert harness.runtime.model == "gemma4:e2b"
    assert preloaded == ["gemma4:e2b"]


def test_model_use_without_preload_callback_works(tmp_path: Path) -> None:
    """/model use works fine when no preload callable is provided (default None)."""
    from shellpilot.llm.ollama import LocalModel

    harness = Harness(tmp_path)
    harness.fake.models.append(LocalModel(name="gemma4:e2b", size_bytes=2_500_000_000))
    # No _preload set — should not raise.
    harness.dispatcher.handle("/model use gemma4:e2b")
    assert harness.runtime.model == "gemma4:e2b"


def test_clear_with_active_plan_reports_cancellation(tmp_path: Path) -> None:
    """After /clear with an active plan the console mentions the plan was cancelled."""
    from tests.fakes.fake_llm import tool_call as tc

    harness = Harness(tmp_path, confirm_answer=True)
    # Seed an active plan by driving a propose_plan call through the runtime.
    # After approval step 1 becomes active; three plain-text answers exhaust
    # the two nudges and then end the turn.
    harness.fake.script = [
        tc(
            "propose_plan",
            goal="A plan",
            steps=["Alpha", "Beta", "Gamma"],
            assumptions=[],
            verification=[],
        ),
        answer("Starting."),
        answer("Still starting."),
        answer("Stopping for now."),
    ]
    harness.runtime._ui = FakeUI(plan_answer=("y", ""))  # type: ignore[attr-defined]
    harness.runtime.run_turn("Do the plan")

    assert harness.runtime.plan_manager.active is not None
    harness.dispatcher.handle("/clear")

    out = harness.output()
    assert "plan" in out.lower()
    assert "cancel" in out.lower()


# ---------------------------------------------------------------------------
# B9: /attach command
# ---------------------------------------------------------------------------


class AttachHarness(Harness):
    """Harness with an AttachmentQueue injected into the dispatcher."""

    def __init__(self, tmp_path: Path) -> None:
        from shellpilot.cli.attachments import AttachmentQueue

        super().__init__(tmp_path)
        self.attachments: AttachmentQueue = AttachmentQueue()
        self.dispatcher._attachments = self.attachments  # type: ignore[attr-defined]

    def build(self, tmp_path: Path, *, vision: bool = True) -> None:
        """Rebuild dispatcher with a vision-capable or vision-lacking FakeLLM."""
        caps = ("completion", "tools", "vision") if vision else ("completion", "tools")
        self.fake.capabilities = caps


def test_attach_stages_and_reports(tmp_path: Path) -> None:
    """/attach <valid-image> stages the file and prints its name + 'next message'."""
    from shellpilot.cli.attachments import AttachmentQueue
    from tests.conftest import TINY_PNG

    img = tmp_path / "photo.png"
    img.write_bytes(TINY_PNG)

    attachments: AttachmentQueue = AttachmentQueue()
    harness = Harness(tmp_path)
    harness.dispatcher._attachments = attachments  # type: ignore[attr-defined]

    harness.dispatcher.handle(f"/attach {img}")

    out = harness.output()
    assert "photo.png" in out
    assert "next message" in out.lower()
    # The path was queued
    assert attachments.paths == [img]


def test_attach_rejects_non_vision_model(tmp_path: Path) -> None:
    """/attach on a non-vision model prints a friendly error and does NOT stage."""
    from shellpilot.cli.attachments import AttachmentQueue
    from tests.conftest import TINY_PNG

    img = tmp_path / "photo.png"
    img.write_bytes(TINY_PNG)

    attachments: AttachmentQueue = AttachmentQueue()
    harness = Harness(tmp_path)
    harness.fake.capabilities = ("completion", "tools")  # no vision
    harness.dispatcher._attachments = attachments  # type: ignore[attr-defined]

    harness.dispatcher.handle(f"/attach {img}")

    out = harness.output()
    # Should mention the model and suggest /model use
    assert "/model use" in out
    # Nothing staged
    assert attachments.paths == []


def test_attach_bare_lists_staged(tmp_path: Path) -> None:
    """Bare /attach lists staged files (or 'No attachments staged')."""
    from shellpilot.cli.attachments import AttachmentQueue

    attachments: AttachmentQueue = AttachmentQueue()
    harness = Harness(tmp_path)
    harness.dispatcher._attachments = attachments  # type: ignore[attr-defined]

    # No files staged yet
    harness.dispatcher.handle("/attach")
    out = harness.output()
    assert "no attachments" in out.lower()

    # Stage a phantom path directly (list, not validate)
    attachments.paths.append(tmp_path / "img.png")
    harness.dispatcher.handle("/attach")
    out2 = harness.output()
    assert "img.png" in out2


# ---------------------------------------------------------------------------
# Fix 2: /cwd propagation — runtime returns updated workspace
# ---------------------------------------------------------------------------


def test_cwd_set_reflected_in_runtime_status(tmp_path: Path) -> None:
    """After /cwd set the runtime.status().workspace is the new path.

    This verifies the seam that terminal.py's MANUAL_SHELL branch now reads
    from (runtime.status().workspace) instead of a stale local variable.
    """
    new_workspace = tmp_path / "live"
    new_workspace.mkdir()
    harness = Harness(tmp_path, confirm_answer=True)
    harness.dispatcher.handle(f"/cwd set {new_workspace}")
    assert harness.runtime.status().workspace == new_workspace.resolve()


# ---------------------------------------------------------------------------
# Fix 3: /logs scoped to current session
# ---------------------------------------------------------------------------


def _make_audit_harness(tmp_path: Path) -> tuple["Harness", object]:
    """Return a Harness with a real AuditLogger wired into the runtime."""
    from shellpilot.persistence.audit_store import AuditLogger

    harness = Harness(tmp_path)
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id=harness.runtime.audit.session_id  # type: ignore[union-attr]
        if harness.runtime.audit is not None
        else "test-session",
        workspace=tmp_path,
        profile="balanced",
    )
    harness.runtime._audit = audit  # type: ignore[attr-defined]
    harness.dispatcher._runtime = harness.runtime  # type: ignore[attr-defined]
    return harness, audit


def test_logs_default_shows_only_current_session(tmp_path: Path) -> None:
    """/logs without args shows only events for the current session."""
    from shellpilot.persistence.audit_store import AuditLogger

    # Two loggers writing to the same file — different session ids.
    path = tmp_path / "audit.jsonl"
    logger_other = AuditLogger(
        path=path, session_id="other-session", workspace=tmp_path, profile="balanced"
    )
    logger_current = AuditLogger(
        path=path, session_id="current-session", workspace=tmp_path, profile="balanced"
    )
    logger_other.write("user_turn", chars=1)
    logger_current.write("user_turn", chars=2)
    logger_other.write("session_end")

    harness = Harness(tmp_path)
    harness.runtime._audit = logger_current  # type: ignore[attr-defined]

    harness.dispatcher.handle("/logs")
    out = harness.output()
    # The current session's event must appear; the other session's must not.
    assert "current-session" not in out or "user_turn" in out
    assert "other-session" not in out


def test_logs_all_shows_events_from_all_sessions(tmp_path: Path) -> None:
    """/logs all shows audit events regardless of session."""
    from shellpilot.persistence.audit_store import AuditLogger

    path = tmp_path / "audit.jsonl"
    AuditLogger(path=path, session_id="session-x", workspace=tmp_path, profile="balanced").write(
        "user_turn", chars=1
    )
    logger_current = AuditLogger(
        path=path, session_id="session-y", workspace=tmp_path, profile="balanced"
    )
    logger_current.write("user_turn", chars=2)

    harness = Harness(tmp_path)
    harness.runtime._audit = logger_current  # type: ignore[attr-defined]

    harness.dispatcher.handle("/logs all")
    out = harness.output()
    # Both session events are visible (both are "user_turn" events in the tail).
    assert out.count("user_turn") == 2


def test_attach_rejects_bad_file(tmp_path: Path) -> None:
    """/attach on a non-image file prints an error and does NOT stage."""
    from shellpilot.cli.attachments import AttachmentQueue

    bad = tmp_path / "data.csv"
    bad.write_text("a,b,c")

    attachments: AttachmentQueue = AttachmentQueue()
    harness = Harness(tmp_path)
    harness.dispatcher._attachments = attachments  # type: ignore[attr-defined]

    harness.dispatcher.handle(f"/attach {bad}")

    out = harness.output()
    # The error names the failure and the unsupported extension; nothing staged.
    assert "Cannot attach" in out
    assert ".csv" in out
    assert attachments.paths == []


# ---------------------------------------------------------------------------
# Task 2: /config set | unset | reset
# ---------------------------------------------------------------------------


def _make_audit_harness_with_file(tmp_path: Path) -> tuple["Harness", "object"]:
    """Return a Harness with a real AuditLogger so audit events are written."""
    from shellpilot.persistence.audit_store import AuditLogger

    harness = Harness(tmp_path)
    audit = AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="test-session",
        workspace=tmp_path,
        profile="balanced",
        redact=False,
    )
    harness.runtime._audit = audit  # type: ignore[attr-defined]
    return harness, audit


def test_config_set_live_key_updates_runtime(tmp_path: Path) -> None:
    """/config set runtime.max_tool_turns 20 persists and updates the runtime."""
    import json

    harness = Harness(tmp_path)
    assert harness.runtime.settings.runtime.max_tool_turns != 20

    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")

    # Runtime is updated immediately.
    assert harness.runtime.settings.runtime.max_tool_turns == 20

    # overrides.json contains the value.
    overrides_file = tmp_path / "overrides.json"
    assert overrides_file.is_file()
    data = json.loads(overrides_file.read_text())
    assert data["runtime.max_tool_turns"] == 20

    # A fresh load_config against the same paths resolves 20 with source "set".
    from shellpilot.config.loader import load_config

    fresh = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
        overrides_file=overrides_file,
    )
    assert fresh.settings.runtime.max_tool_turns == 20
    assert fresh.sources["runtime.max_tool_turns"] == "set"


def test_config_set_value_join(tmp_path: Path) -> None:
    """/config set joins multi-token values so spaces are preserved for str keys."""
    harness = Harness(tmp_path)
    # model.keep_alive is a str field; "10m" is a single token but the
    # args join behaviour is what we're verifying here.
    harness.dispatcher.handle("/config set model.keep_alive 10m")
    assert harness.runtime.settings.model.keep_alive == "10m"


def test_config_set_boot_only_key_prints_next_session(tmp_path: Path) -> None:
    """/config set on a boot-only key prints a 'takes effect next session' note."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set ui.spinner false")
    out = harness.output()
    assert "next session" in out


def test_config_set_model_default_mentions_model_use(tmp_path: Path) -> None:
    """/config set model.default also mentions /model use <name>."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set model.default gemma4:e2b")
    out = harness.output()
    assert "next session" in out
    assert "/model use" in out


def test_config_set_invalid_value_rejected(tmp_path: Path) -> None:
    """/config set with an out-of-range value prints an error and saves nothing."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 0")
    out = harness.output()
    assert "error" in out.lower() or "must be" in out
    # overrides.json must not exist (nothing was saved).
    assert not (tmp_path / "overrides.json").exists()


def test_config_set_unknown_key_rejected(tmp_path: Path) -> None:
    """/config set on an unknown key prints an error with a did-you-mean hint."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_tuurns 10")
    out = harness.output()
    assert "error" in out.lower() or "unknown" in out.lower()
    # overrides.json must not exist.
    assert not (tmp_path / "overrides.json").exists()


def test_config_set_model_options_rejected(tmp_path: Path) -> None:
    """/config set model.options is rejected (config-file only)."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set model.options {}")
    out = harness.output()
    assert "error" in out.lower() or "config-file only" in out.lower()
    assert not (tmp_path / "overrides.json").exists()


def test_config_unset_existing_reverts(tmp_path: Path) -> None:
    """/config unset removes the override and shows the reverted source."""
    import json

    harness = Harness(tmp_path)
    # Set first.
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    assert harness.runtime.settings.runtime.max_tool_turns == 20

    # Unset.
    harness.dispatcher.handle("/config unset runtime.max_tool_turns")
    # Should revert to default (40).
    assert harness.runtime.settings.runtime.max_tool_turns == 40
    # Output should mention the source (default).
    out = harness.output()
    assert "default" in out
    # overrides.json should no longer contain the key.
    data = json.loads((tmp_path / "overrides.json").read_text())
    assert "runtime.max_tool_turns" not in data


def test_config_unset_absent_key_reports_noop(tmp_path: Path) -> None:
    """/config unset on a key with no override prints a no-op message."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config unset runtime.max_tool_turns")
    out = harness.output()
    assert "no override" in out.lower()


def test_config_reset_alias_for_unset(tmp_path: Path) -> None:
    """/config reset <key> is an alias for /config unset <key>."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    assert harness.runtime.settings.runtime.max_tool_turns == 20
    harness.dispatcher.handle("/config reset runtime.max_tool_turns")
    assert harness.runtime.settings.runtime.max_tool_turns == 40


def test_config_reset_all_declined(tmp_path: Path) -> None:
    """/config reset (no key) declined → overrides.json unchanged."""
    import json

    harness = Harness(tmp_path, confirm_answer=False)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    # Patch confirm to decline.
    harness.dispatcher._confirm = lambda _: False  # type: ignore[method-assign]
    harness.dispatcher.handle("/config reset")
    out = harness.output()
    assert "cancelled" in out.lower()
    # File should still have the entry.
    data = json.loads((tmp_path / "overrides.json").read_text())
    assert "runtime.max_tool_turns" in data


def test_config_reset_all_accepted_clears(tmp_path: Path) -> None:
    """/config reset (no key) accepted → overrides.json empty, count reported."""
    import json

    harness = Harness(tmp_path, confirm_answer=True)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    harness.dispatcher.handle("/config set runtime.max_plan_steps 5")
    harness.dispatcher._confirm = lambda _: True  # type: ignore[method-assign]
    harness.dispatcher.handle("/config reset")
    out = harness.output()
    # "cleared N override(s)" message.
    assert "cleared" in out
    assert "2" in out
    data = json.loads((tmp_path / "overrides.json").read_text())
    assert data == {}


def test_config_set_audit_event_written(tmp_path: Path) -> None:
    """/config set writes a config_change audit event."""
    import json

    harness, _ = _make_audit_harness_with_file(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")

    audit_file = tmp_path / "audit.jsonl"
    events = [json.loads(line) for line in audit_file.read_text().splitlines()]
    config_events = [e for e in events if e.get("event") == "config_change"]
    assert any(
        e.get("setting") == "runtime.max_tool_turns" and e.get("source") == "set"
        for e in config_events
    )


def test_config_unset_audit_event_written(tmp_path: Path) -> None:
    """/config unset writes a config_change audit event with value='unset'."""
    import json

    harness, _ = _make_audit_harness_with_file(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    harness.dispatcher.handle("/config unset runtime.max_tool_turns")

    audit_file = tmp_path / "audit.jsonl"
    events = [json.loads(line) for line in audit_file.read_text().splitlines()]
    config_events = [e for e in events if e.get("event") == "config_change"]
    assert any(
        e.get("setting") == "runtime.max_tool_turns" and e.get("value") == "unset"
        for e in config_events
    )


def test_config_reset_all_audit_event_written(tmp_path: Path) -> None:
    """/config reset (all) writes a config_change audit event with source='reset'."""
    import json

    harness, _ = _make_audit_harness_with_file(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    harness.dispatcher._confirm = lambda _: True  # type: ignore[method-assign]
    harness.dispatcher.handle("/config reset")

    audit_file = tmp_path / "audit.jsonl"
    events = [json.loads(line) for line in audit_file.read_text().splitlines()]
    config_events = [e for e in events if e.get("event") == "config_change"]
    assert any(e.get("source") == "reset" for e in config_events)


def test_config_show_override_renders_source_set(tmp_path: Path) -> None:
    """After /config set, /config show renders the key with source 'set'."""
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/config set runtime.max_tool_turns 20")
    harness.dispatcher.handle("/config show")
    out = harness.output()
    assert "set" in out
    assert "runtime.max_tool_turns" in out


def test_config_warnings_printed_on_reload(tmp_path: Path) -> None:
    """If overrides.json is corrupt, /config reload prints the warning."""
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text("not valid json")

    harness = Harness(tmp_path)
    # Patch reload_config to use the real overrides file at tmp_path.
    from shellpilot.config.loader import load_config

    def _reload() -> LoadedConfig:
        return load_config(
            user_config_file=tmp_path / "missing-user.toml",
            project_config_file=tmp_path / "missing-project.toml",
            env={},
            overrides_file=overrides_file,
        )

    harness.dispatcher._reload_config = _reload  # type: ignore[method-assign]
    harness.dispatcher.handle("/config reload")
    out = harness.output()
    assert "invalid JSON" in out or "ignored" in out


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------


def _make_skill(
    name: str,
    root: str = "user",
    valid: bool = True,
    error: str = "",
) -> "object":
    from shellpilot.skills.model import Skill, SkillTrigger

    return Skill(
        name=name,
        description="A skill.",
        body="Do the work.",
        root=root,
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=5,
        valid=valid,
        error=error,
    )


def test_skills_no_skills_shows_friendly_message(tmp_path: Path) -> None:
    """/skills with no discovered skills shows a friendly empty message."""
    harness = Harness(tmp_path)
    # skills is empty by default (no skills= passed to ConversationRuntime)
    harness.dispatcher.handle("/skills")
    out = harness.output()
    assert "No skills discovered" in out


def test_skills_renders_name_root_and_status(tmp_path: Path) -> None:
    """/skills shows skill name, root, and enabled/disabled status."""
    from shellpilot.skills.model import Skill, SkillTrigger

    skill = Skill(
        name="my-skill",
        description="A useful skill.",
        body="Instructions.",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=5,
        valid=True,
    )
    harness = Harness(tmp_path)
    harness.runtime._skills = (skill,)  # type: ignore[attr-defined]
    harness.dispatcher.handle("/skills")
    out = harness.output()
    assert "my-skill" in out
    assert "user" in out


def test_skills_invalid_skill_shows_invalid_status(tmp_path: Path) -> None:
    """/skills marks invalid skills with their error reason."""
    from shellpilot.skills.model import Skill, SkillTrigger

    skill = Skill(
        name="broken",
        description="",
        body="",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=0,
        valid=False,
        error="reserved builtin name",
    )
    harness = Harness(tmp_path)
    harness.runtime._skills = (skill,)  # type: ignore[attr-defined]
    harness.dispatcher.handle("/skills")
    out = harness.output()
    assert "broken" in out
    assert "invalid" in out


def test_skills_renders_decision_resource_script_and_warning_details(tmp_path: Path) -> None:
    """/skills surfaces SkillDecision details instead of re-deriving visibility."""
    from shellpilot.config.model import Settings, SkillSettings
    from shellpilot.skills.model import Skill, SkillResource, SkillScript, SkillTrigger

    skill = Skill(
        name="authoring",
        description="Authoring help.",
        body="Draft carefully.",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=4,
        references=(
            SkillResource(
                kind="reference",
                name="route",
                rel_path="references/route.md",
                text="routing guidance",
                est_tokens=4,
                trigger=SkillTrigger.ENABLED,
            ),
        ),
        scripts=(
            SkillScript(
                name="doctor",
                entry="scripts/doctor.py",
                description="Check files",
                mode="read",
                timeout_seconds=5,
            ),
        ),
        warnings=("scripts/extra.py has no manifest entry",),
    )
    harness = Harness(tmp_path)
    harness.runtime._skills = (skill,)  # type: ignore[attr-defined]
    harness.runtime.update_settings(Settings(skills=SkillSettings(enabled=("authoring",))))

    harness.dispatcher.handle("/skills")

    out = harness.output()
    assert "authoring" in out
    assert "yes" in out
    assert "triggers: enabled" in out
    assert "1 ref injected (route.md)" in out
    assert "1 script (execution unsupported)" in out
    assert "scripts/extra.py has no manifest entry" in out


def test_skills_renders_budget_skip_reason_from_decision(tmp_path: Path) -> None:
    """/skills shows budget skip reasons from the live SkillDecision."""
    from shellpilot.config.model import Settings, SkillSettings
    from shellpilot.skills.model import Skill, SkillTrigger

    skill = Skill(
        name="large",
        description="Large skill.",
        body="Instructions.",
        root="user",
        triggers=(SkillTrigger.ENABLED,),
        est_tokens=10_000,
        valid=True,
    )
    harness = Harness(tmp_path)
    harness.runtime._skills = (skill,)  # type: ignore[attr-defined]
    harness.runtime.update_settings(Settings(skills=SkillSettings(enabled=("large",))))

    harness.dispatcher.handle("/skills")

    out = harness.output()
    assert "large" in out
    assert "no" in out
    assert "skipped: skill budget" in out
