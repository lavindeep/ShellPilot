"""Tests for memory tools, the proposal approval flow, and slash commands (section 16.3)."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from shellpilot.cli.slash import SlashDispatcher
from shellpilot.cli.theme import SHELLPILOT_THEME
from shellpilot.config.loader import LoadedConfig, load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.store import MemoryStore, MemoryStores, project_id_for
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_stores(tmp_path: Path) -> MemoryStores:
    return MemoryStores(
        global_store=MemoryStore(tmp_path / "global-memory.json"),
        project_store=MemoryStore(
            tmp_path / ".shellpilot" / "memory.json", project_id=project_id_for(tmp_path)
        ),
    )


def make_runtime(
    tmp_path: Path, stores: MemoryStores, script: list[object], *, approve: bool = True
) -> tuple[ConversationRuntime, FakeUI, FakeLLM]:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    fake = FakeLLM(script=script)
    ui = FakeUI(approve_actions=approve)
    runtime = ConversationRuntime(
        llm=fake,
        settings=loaded.settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        memory=stores,
    )
    return runtime, ui, fake


def test_memory_read_runs_without_approval(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    stores.global_store.add_preference("Be brief.", scope="global", source="user")
    runtime, ui, _ = make_runtime(tmp_path, stores, [tool_call("memory_read"), answer("done")])
    runtime.run_turn("what do you remember?")
    assert ui.approval_requests == []
    assert any(success for _, success, _ in ui.tool_results)


def test_propose_add_preference_requires_approval_and_persists(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    runtime, ui, _ = make_runtime(
        tmp_path,
        stores,
        [
            tool_call(
                "memory_propose_update",
                action="add_preference",
                text="Always run tests after edits.",
                scope="global",
            ),
            answer("saved"),
        ],
    )
    runtime.run_turn("remember to always run tests")
    assert len(ui.approval_requests) == 1
    request = ui.approval_requests[0]
    assert "Always run tests after edits." in request.diff
    assert stores.global_store.preferences[0].text == "Always run tests after edits."
    assert stores.global_store.preferences[0].source == "assistant"


def test_propose_rejected_leaves_store_untouched(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    runtime, ui, _ = make_runtime(
        tmp_path,
        stores,
        [
            tool_call(
                "memory_propose_update",
                action="add_preference",
                text="Never ask questions.",
                scope="global",
            ),
            answer("ok"),
        ],
        approve=False,
    )
    runtime.run_turn("remember this")
    assert len(ui.approval_requests) == 1
    assert stores.global_store.preferences == ()


def test_propose_fact_and_forget(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    runtime, _, _ = make_runtime(
        tmp_path,
        stores,
        [
            tool_call(
                "memory_propose_update",
                action="add_fact",
                kind="command",
                label="Run unit tests",
                value="python -m pytest",
            ),
            answer("noted"),
        ],
    )
    runtime.run_turn("remember the test command")
    assert stores.project_store.facts[0].value == "python -m pytest"

    fact_id = stores.project_store.facts[0].id
    runtime2, _, _ = make_runtime(
        tmp_path,
        stores,
        [tool_call("memory_propose_update", action="forget", id=fact_id), answer("gone")],
    )
    runtime2.run_turn("forget the test command")
    assert stores.project_store.facts == ()


class MemoryHarness:
    def __init__(self, tmp_path: Path, script: list[object], confirm: bool = True) -> None:
        self.console = Console(record=True, width=100, theme=SHELLPILOT_THEME)
        self.stores = make_stores(tmp_path)
        self.runtime, self.ui, self.fake = make_runtime(tmp_path, self.stores, script)
        self.dispatcher = SlashDispatcher(
            runtime=self.runtime,
            client=self.fake,
            console=self.console,
            loaded=self._loaded(tmp_path),
            user_config_file=tmp_path / "config.toml",
            reload_config=lambda: self._loaded(tmp_path),
            confirm=lambda prompt: confirm,
        )

    @staticmethod
    def _loaded(tmp_path: Path) -> LoadedConfig:
        return load_config(
            user_config_file=tmp_path / "missing-user.toml",
            project_config_file=tmp_path / "missing-project.toml",
            env={},
        )

    def output(self) -> str:
        return self.console.export_text()


def test_slash_memory_show_add_forget(tmp_path: Path) -> None:
    harness = MemoryHarness(tmp_path, [])
    harness.dispatcher.handle("/memory show")
    assert "No stored memory" in harness.output()

    harness.dispatcher.handle("/memory add Prefer uv over pip")
    assert harness.stores.global_store.preferences[0].text == "Prefer uv over pip"
    assert harness.stores.global_store.preferences[0].source == "user"

    pref_id = harness.stores.global_store.preferences[0].id
    harness.dispatcher.handle(f"/memory forget {pref_id}")
    assert harness.stores.global_store.preferences == ()


def test_slash_prefs_show_and_edit(tmp_path: Path) -> None:
    harness = MemoryHarness(tmp_path, [])
    harness.stores.global_store.add_preference("Be concise.", scope="global", source="user")
    harness.dispatcher.handle("/prefs show")
    assert "Be concise." in harness.output()
    harness.dispatcher.handle("/prefs edit")
    assert "memory.json" in harness.output()


def test_memory_compact_merges_with_model_and_keeps_user_entries(tmp_path: Path) -> None:
    script = [answer('[{"id": "pref_001", "text": "Be concise and ask before installing."}]')]
    harness = MemoryHarness(tmp_path, script)
    harness.stores.global_store.add_preference("Be concise.", scope="global", source="user")
    harness.stores.global_store.add_preference(
        "Ask before installing dependencies.", scope="global", source="assistant"
    )
    harness.dispatcher.handle("/memory compact")
    prefs = harness.stores.global_store.preferences
    assert len(prefs) == 1
    assert prefs[0].id == "pref_001"
    assert "Be concise and ask before installing." in prefs[0].text


def test_memory_compact_refuses_to_drop_user_entries(tmp_path: Path) -> None:
    # The model keeps only the assistant entry, trying to drop the user's.
    script = [answer('[{"id": "pref_002", "text": "Ask before installing."}]')]
    harness = MemoryHarness(tmp_path, script)
    harness.stores.global_store.add_preference("Be concise.", scope="global", source="user")
    harness.stores.global_store.add_preference(
        "Ask before installing dependencies.", scope="global", source="assistant"
    )
    harness.dispatcher.handle("/memory compact")
    assert len(harness.stores.global_store.preferences) == 2  # unchanged
    assert "user entries" in harness.output()
