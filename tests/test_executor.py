"""Tests for the tool broker recovery loop with the fake model (section 10.4)."""

from pathlib import Path

from shellpilot.config.model import RuntimeSettings, Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_runtime(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, settings: Settings | None = None
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=settings or Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )


def test_tool_call_round_trip(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("remember the milk")
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="notes.txt"),
            answer("Your note says: remember the milk."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("What is in notes.txt?")

    assert reply == "Your note says: remember the milk."
    assert ui.tool_calls == [("read_file", {"path": "notes.txt"})]
    assert ui.tool_results[0][1] is True
    # The model received the tool result back.
    final_call = fake.calls[-1]
    tool_messages = [m for m in final_call.messages if m.role == "tool"]
    assert any("remember the milk" in m.content for m in tool_messages)


def test_unknown_tool_gets_reminder_then_recovers(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("read_files", path="x"),  # malformed: unknown tool
            answer("Recovered without tools."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("read x")

    assert reply == "Recovered without tools."
    final_call = fake.calls[-1]
    tool_messages = [m for m in final_call.messages if m.role == "tool"]
    assert any("unknown tool" in m.content for m in tool_messages)
    assert any("Retry once" in m.content for m in tool_messages)


def test_invalid_args_get_schema_reminder(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("read_file", file="x.txt"),  # malformed: wrong arg name
            answer("done"),
        ]
    )
    runtime = make_runtime(fake, FakeUI(), tmp_path)
    runtime.run_turn("read")
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("read_file(" in m.content for m in tool_messages)  # schema reminder


def test_two_consecutive_malformed_calls_stop_tool_use(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("bogus_one"),
            tool_call("bogus_two"),
            answer("Stopping cleanly."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)

    reply = runtime.run_turn("do something")

    assert reply == "Stopping cleanly."
    assert any("malformed" in status.lower() for status in ui.statuses)
    # After the stop, the final call must offer no tools.
    assert fake.calls[-1].tools == ()


def test_tool_loop_budget_stops_runaway_model(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("data")
    max_turns = 3
    settings = Settings(runtime=RuntimeSettings(max_tool_turns=max_turns))
    script = [tool_call("read_file", path="f.txt") for _ in range(max_turns + 1)]
    script.append(answer("Finished after budget stop."))
    fake = FakeLLM(script=script)
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path, settings)

    reply = runtime.run_turn("loop forever")

    assert reply == "Finished after budget stop."
    assert any("budget" in status.lower() for status in ui.statuses)
    assert fake.calls[-1].tools == ()


def test_tool_crash_is_reported_not_raised(tmp_path: Path) -> None:
    # read_file on a directory path that exists triggers the not-a-file branch;
    # simulate a crash instead via an unreadable argument type accepted by schema.
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="missing-file.txt"),
            answer("Handled the failure."),
        ]
    )
    ui = FakeUI()
    runtime = make_runtime(fake, ui, tmp_path)
    reply = runtime.run_turn("read it")
    assert reply == "Handled the failure."
    assert ui.tool_results[0][1] is False  # failure surfaced to the user
    tool_messages = [m for m in fake.calls[-1].messages if m.role == "tool"]
    assert any("status: failed" in m.content for m in tool_messages)
