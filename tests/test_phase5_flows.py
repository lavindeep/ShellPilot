"""Phase 5 flows: high-risk explanation, audit wiring, profile switching, manual shell."""

import json
from pathlib import Path

from rich.console import Console

from shellpilot.cli.manual_shell import BANNER, manual_shell_loop, run_manual_command
from shellpilot.config.model import Settings
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.policy.explanations import explain_risk
from shellpilot.runtime.conversation import ConversationRuntime
from tests.fakes.fake_llm import FakeLLM, answer, tool_call
from tests.fakes.fake_ui import FakeUI


def make_audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(
        path=tmp_path / "audit.jsonl",
        session_id="test",
        workspace=tmp_path,
        profile="balanced",
    )


def make_runtime(
    fake: FakeLLM, ui: FakeUI, tmp_path: Path, audit: AuditLogger
) -> ConversationRuntime:
    return ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
        audit=audit,
    )


def read_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_high_risk_command_gets_deterministic_explanation(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            tool_call("run_command", argv=["rm", "-rf", "build"]),
            answer("Done."),
        ]
    )
    ui = FakeUI(approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path, make_audit(tmp_path))
    (tmp_path / "build").mkdir()

    text = runtime.run_turn("clean the build dir")

    request = ui.approval_requests[0]
    assert request.risk.value == "high"
    assert request.purpose == explain_risk(("recursive delete",))
    assert "permanently deletes" in request.purpose
    assert text == "Done."
    # No extra explainer model round-trip: exactly the two scripted calls ran.
    assert len(fake.calls) == 2
    events = read_events(tmp_path)
    approval = next(e for e in events if e["event"] == "approval")
    assert approval["decision"] == "approved"
    assert approval["risk"] == "high"
    assert "permanently deletes" in str(approval["explanation"])


def test_audit_records_turn_tool_and_result(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("data")
    fake = FakeLLM(script=[tool_call("read_file", path="f.txt"), answer("read it")])
    runtime = make_runtime(fake, FakeUI(), tmp_path, make_audit(tmp_path))

    runtime.run_turn("read f.txt")

    events = read_events(tmp_path)
    kinds = [e["event"] for e in events]
    assert "user_turn" in kinds
    assert "tool_result" in kinds


def test_file_edit_event_recorded(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old\n")
    fake = FakeLLM(
        script=[
            tool_call("read_file", path="f.txt"),
            tool_call("patch_file", path="f.txt", operation="replace_exact", old="old", new="new"),
            answer("edited"),
        ]
    )
    ui = FakeUI(approve_actions=True)
    runtime = make_runtime(fake, ui, tmp_path, make_audit(tmp_path))

    runtime.run_turn("change old to new")

    events = read_events(tmp_path)
    assert any(e["event"] == "file_edit" for e in events)
    assert (tmp_path / "f.txt").read_text() == "new\n"


# -- manual shell ---------------------------------------------------------------


def test_manual_command_runs_and_audits(tmp_path: Path) -> None:
    audit = make_audit(tmp_path)
    exit_code = run_manual_command("true", tmp_path, audit)
    assert exit_code == 0
    events = read_events(tmp_path)
    assert events[0]["event"] == "manual_shell_command"
    assert events[0]["risk"] == "raw_shell"
    assert events[0]["exit_code"] == 0


def test_manual_shell_loop_banner_and_exit(tmp_path: Path) -> None:
    console = Console(record=True, width=100)
    audit = make_audit(tmp_path)
    lines = iter(["echo manual-mode-test > probe.txt", "/exit-shell"])

    manual_shell_loop(console, tmp_path, audit, read_line=lambda: next(lines))

    output = console.export_text()
    assert "Manual Shell" in output
    assert "The AI is not controlling this mode" in output
    assert (tmp_path / "probe.txt").read_text().strip() == "manual-mode-test"
    kinds = [e["event"] for e in read_events(tmp_path)]
    assert kinds == ["manual_shell_enter", "manual_shell_command", "manual_shell_exit"]


def test_banner_matches_design_wording() -> None:
    assert "shell=True" in BANNER
    assert "/exit-shell" in BANNER
