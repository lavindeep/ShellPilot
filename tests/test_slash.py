"""Tests for slash-command dispatch."""

from pathlib import Path

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


def test_model_list_filters_to_family(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dispatcher.handle("/model list")
    assert "gemma4:e4b" in harness.output()


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
