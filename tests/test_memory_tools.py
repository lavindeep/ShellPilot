"""Tests for memory tools, the proposal approval flow, and slash commands (section 16.3)."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from shellpilot.cli.slash import SlashDispatcher
from shellpilot.cli.theme import SHELLPILOT_THEME
from shellpilot.config.loader import LoadedConfig, load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.store import MAX_MEMORY_FILE_CHARS, MemoryStore, MemoryStores, project_id_for
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


def test_memory_propose_update_schema_explains_scope_policy(tmp_path: Path) -> None:
    stores = make_stores(tmp_path)
    runtime, _, _ = make_runtime(tmp_path, stores, [])
    tool = runtime.registry.get("memory_propose_update")
    assert tool is not None
    description = tool.definition.description

    assert "Global memory: durable user facts" in description
    assert "preferred languages/tools" in description
    assert "Project memory: current workspace" in description
    assert "repo commands, paths, architecture" in description
    assert "If scope is ambiguous" in description


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


def test_slash_memory_add_reports_file_cap(tmp_path: Path) -> None:
    harness = MemoryHarness(tmp_path, [])
    harness.dispatcher.handle(f"/memory add {'x' * MAX_MEMORY_FILE_CHARS}")

    output = harness.output()
    assert "limit is" in output
    assert str(MAX_MEMORY_FILE_CHARS) in output
    assert harness.stores.global_store.preferences == ()


def test_slash_memory_forget_shrinks_over_cap_legacy_store(tmp_path: Path) -> None:
    harness = MemoryHarness(tmp_path, [])
    legacy_entries = [
        {
            "id": f"pref_{index:03d}",
            "scope": "global",
            "text": f"legacy preference {index} " + ("x" * 220),
            "source": "user",
            "updated_at": "2026-07-04T00:00:00Z",
        }
        for index in range(1, 21)
    ]
    harness.stores.global_store.path.write_text(
        json.dumps({"version": 1, "preferences": legacy_entries, "facts": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    before_size = len(harness.stores.global_store.path.read_text(encoding="utf-8"))
    assert before_size > MAX_MEMORY_FILE_CHARS
    harness.stores.global_store.reload()

    harness.dispatcher.handle("/memory forget pref_001")

    after = harness.stores.global_store.path.read_text(encoding="utf-8")
    assert len(after) < before_size
    assert len(after) > MAX_MEMORY_FILE_CHARS
    assert "Removed pref_001." in harness.output()
    assert "pref_001" not in {
        preference.id for preference in harness.stores.global_store.preferences
    }


def test_memory_show_folds_in_scope_source_and_prefs_retired(tmp_path: Path) -> None:
    """/prefs was retired into /memory show (v0.10.0): the preference view now
    carries the (scope, source) tag /prefs printed, and /prefs is gone."""
    harness = MemoryHarness(tmp_path, [])
    harness.stores.global_store.add_preference("Be concise.", scope="global", source="user")
    harness.dispatcher.handle("/memory show")
    out = harness.output()
    assert "Be concise." in out
    assert "(global, user)" in out  # folded-in tag, was /prefs show
    harness.dispatcher.handle("/prefs show")
    assert "Unknown command: /prefs" in harness.output()


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


def test_memory_compact_reports_file_cap(tmp_path: Path) -> None:
    oversized = "x" * MAX_MEMORY_FILE_CHARS
    script = [answer(json.dumps([{"id": "pref_001", "text": oversized}]))]
    harness = MemoryHarness(tmp_path, script)
    harness.stores.global_store.add_preference(
        "First assistant entry.", scope="global", source="assistant"
    )
    harness.stores.global_store.add_preference(
        "Second assistant entry.", scope="global", source="assistant"
    )

    harness.dispatcher.handle("/memory compact")

    output = harness.output()
    assert "limit is" in output
    assert str(MAX_MEMORY_FILE_CHARS) in output
    assert [p.text for p in harness.stores.global_store.preferences] == [
        "First assistant entry.",
        "Second assistant entry.",
    ]


def test_memory_compact_rejects_atomically_when_later_store_exceeds_cap(tmp_path: Path) -> None:
    oversized = "x" * MAX_MEMORY_FILE_CHARS
    script = [
        answer(
            json.dumps(
                [
                    {"id": "pref_001", "text": "Global changed."},
                    {"id": "pref_900", "text": oversized},
                ]
            )
        )
    ]
    harness = MemoryHarness(tmp_path, script)
    harness.stores.global_store.add_preference(
        "Global original.", scope="global", source="assistant"
    )
    harness.stores.project_store.path.parent.mkdir(parents=True, exist_ok=True)
    harness.stores.project_store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "project_id": project_id_for(tmp_path),
                "preferences": [
                    {
                        "id": "pref_900",
                        "scope": "project",
                        "text": "Project original.",
                        "source": "assistant",
                        "updated_at": "2026-07-04T00:00:00Z",
                    }
                ],
                "facts": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    harness.stores.project_store.reload()

    harness.dispatcher.handle("/memory compact")

    output = harness.output()
    assert "limit is" in output
    assert str(MAX_MEMORY_FILE_CHARS) in output
    assert harness.stores.global_store.preferences[0].text == "Global original."
    assert harness.stores.project_store.preferences[0].text == "Project original."
