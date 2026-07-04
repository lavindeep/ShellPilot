"""Tests for structured memory stores and prompt injection (design section 16)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shellpilot.config.loader import load_config
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.store import (
    MAX_MEMORY_FILE_CHARS,
    MemoryFormatError,
    MemoryStore,
    MemoryStores,
    project_id_for,
)
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer
from tests.fakes.fake_ui import FakeUI


def test_add_save_reload_round_trip(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    pref = store.add_preference("Prefer concise answers.", scope="global", source="user")
    assert pref.id == "pref_001"
    second = store.add_preference("Ask before installing.", scope="global", source="assistant")
    assert second.id == "pref_002"

    reloaded = MemoryStore(tmp_path / "memory.json")
    texts = [p.text for p in reloaded.preferences]
    assert texts == ["Prefer concise answers.", "Ask before installing."]
    assert reloaded.preferences[0].source == "user"


def test_facts_round_trip_with_project_id(tmp_path: Path) -> None:
    pid = project_id_for(tmp_path)
    assert pid.startswith("ShellPilot:")
    store = MemoryStore(tmp_path / ".shellpilot" / "memory.json", project_id=pid)
    fact = store.add_fact(
        kind="command", value="python -m pytest", label="Run unit tests", source="tool_result"
    )
    assert fact.id == "fact_001"
    reloaded = MemoryStore(tmp_path / ".shellpilot" / "memory.json", project_id=pid)
    assert reloaded.facts[0].value == "python -m pytest"
    raw = json.loads((tmp_path / ".shellpilot" / "memory.json").read_text())
    assert raw["project_id"] == pid
    assert raw["version"] == 1


def test_remove_by_id(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    pref = store.add_preference("temp", scope="global", source="user")
    assert store.remove(pref.id) is True
    assert store.remove("pref_404") is False
    assert MemoryStore(tmp_path / "memory.json").preferences == ()


def test_unknown_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    path.write_text(json.dumps({"version": 99, "preferences": []}), encoding="utf-8")
    with pytest.raises(MemoryFormatError):
        MemoryStore(path)


def test_secrets_never_stored(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    store.add_preference(
        "Deploy with token ghp_abcdefghijklmnopqrstuvwxyz0123456789", scope="global", source="user"
    )
    raw = (tmp_path / "memory.json").read_text(encoding="utf-8")
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in raw


def test_memory_file_write_is_capped_to_force_selective_storage(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.json")
    too_large = "x" * MAX_MEMORY_FILE_CHARS

    with pytest.raises(MemoryFormatError, match=f"{MAX_MEMORY_FILE_CHARS} characters"):
        store.add_preference(too_large, scope="global", source="user")

    assert not (tmp_path / "memory.json").exists()


def test_project_memory_file_write_is_capped_separately(tmp_path: Path) -> None:
    store = MemoryStore(
        tmp_path / ".shellpilot" / "memory.json", project_id=project_id_for(tmp_path)
    )
    too_large = "x" * MAX_MEMORY_FILE_CHARS

    with pytest.raises(MemoryFormatError, match=f"{MAX_MEMORY_FILE_CHARS} characters"):
        store.add_fact(kind="note", label="oversized", value=too_large, source="assistant")

    assert not (tmp_path / ".shellpilot" / "memory.json").exists()


def test_over_cap_legacy_store_can_shrink_by_removing_entry(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
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
    path.write_text(
        json.dumps({"version": 1, "preferences": legacy_entries, "facts": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    before_size = len(path.read_text(encoding="utf-8"))
    assert before_size > MAX_MEMORY_FILE_CHARS

    store = MemoryStore(path)

    assert store.remove("pref_001") is True
    after_size = len(path.read_text(encoding="utf-8"))
    assert after_size < before_size
    assert after_size > MAX_MEMORY_FILE_CHARS
    assert "pref_001" not in {preference.id for preference in MemoryStore(path).preferences}


def test_over_cap_legacy_store_still_rejects_growth(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
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
    path.write_text(
        json.dumps({"version": 1, "preferences": legacy_entries, "facts": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    store = MemoryStore(path)

    with pytest.raises(MemoryFormatError, match="Forget or compact"):
        store.add_preference("new entry", scope="global", source="user")

    assert path.read_text(encoding="utf-8") == before


def test_minified_over_cap_legacy_store_can_shrink_by_removing_entry(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
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
    path.write_text(
        json.dumps(
            {"version": 1, "preferences": legacy_entries, "facts": []}, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    raw_size = len(path.read_text(encoding="utf-8"))
    store = MemoryStore(path)
    normalized_size = len(store._payload_text(store._payload()))
    assert raw_size > MAX_MEMORY_FILE_CHARS
    assert normalized_size > raw_size

    assert store.remove("pref_001") is True

    after_size = len(path.read_text(encoding="utf-8"))
    assert after_size < normalized_size
    assert after_size > raw_size
    assert "pref_001" not in {preference.id for preference in MemoryStore(path).preferences}


def test_render_block_lists_entries_and_caps_tokens(tmp_path: Path) -> None:
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json", project_id="ShellPilot:abc")
    global_store.add_preference("Prefer concise answers.", scope="global", source="user")
    project_store.add_fact(
        kind="command", value="python -m pytest", label="Run unit tests", source="user"
    )
    stores = MemoryStores(global_store=global_store, project_store=project_store)
    block = stores.render(max_tokens=500)
    assert "Prefer concise answers." in block
    assert "python -m pytest" in block
    assert "[pref_001]" in block and "[fact_001]" in block

    tiny = stores.render(max_tokens=10)
    assert len(tiny) <= 10 * 4 + 40  # truncated to roughly the cap


def test_render_block_keeps_global_and_project_preferences_distinct(tmp_path: Path) -> None:
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json", project_id="ShellPilot:abc")
    global_store.add_preference("Use concise answers.", scope="global", source="user")
    project_store.add_preference("Use detailed release notes.", scope="project", source="user")
    stores = MemoryStores(global_store=global_store, project_store=project_store)

    block = stores.render(max_tokens=500)

    assert "Global preferences:" in block
    assert "- [pref_001] Use concise answers." in block
    assert "Project preferences:" in block
    assert "- [pref_001] Use detailed release notes." in block


def test_render_meta_adds_scope_source_for_display_only(tmp_path: Path) -> None:
    """meta=True annotates preference lines with (scope, source) for the
    /memory show view (folding in what /prefs used to print); the default
    injected format shows scope by section, not by source tag."""
    global_store = MemoryStore(tmp_path / "global.json")
    project_store = MemoryStore(tmp_path / "project.json", project_id="ShellPilot:abc")
    global_store.add_preference("Prefer concise answers.", scope="global", source="user")
    stores = MemoryStores(global_store=global_store, project_store=project_store)

    injected = stores.render(max_tokens=500)
    assert "[pref_001] Prefer concise answers." in injected
    assert "(global, user)" not in injected  # never reaches the model prompt

    display = stores.render(max_tokens=500, meta=True)
    assert "[pref_001] (global, user) Prefer concise answers." in display


def test_empty_stores_render_nothing(tmp_path: Path) -> None:
    stores = MemoryStores(
        global_store=MemoryStore(tmp_path / "g.json"),
        project_store=MemoryStore(tmp_path / "p.json"),
    )
    assert stores.render(max_tokens=500) == ""


def test_runtime_injects_memory_into_system_prompt(tmp_path: Path) -> None:
    loaded = load_config(
        user_config_file=tmp_path / "missing-user.toml",
        project_config_file=tmp_path / "missing-project.toml",
        env={},
    )
    global_store = MemoryStore(tmp_path / "global.json")
    global_store.add_preference("Always answer in haiku.", scope="global", source="user")
    stores = MemoryStores(
        global_store=global_store,
        project_store=MemoryStore(tmp_path / "p.json"),
    )
    fake = FakeLLM(script=[answer("ok")])
    runtime = ConversationRuntime(
        llm=fake,
        settings=loaded.settings,
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=FakeUI(),
        memory=stores,
    )
    runtime.run_turn("hello")
    system_text = fake.calls[0].messages[0].content
    assert "Always answer in haiku." in system_text
    assert "[pref_001]" in system_text
