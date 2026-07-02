"""Tests for the worker-thread turn + loop-thread marshaling (design section 31.13).

Branch 4. These drive ``ThreadedUI`` / ``TurnRunner`` with an injected
*synchronous* schedule (a list/queue that captures the callables) so the
cross-thread handoff is exercised for real (a worker thread runs the turn) yet
drained deterministically on the test thread after ``join``.

What CANNOT be covered here (needs live test, handed to the user):
- the real ``app.run()`` event loop and ``call_soon_threadsafe`` repaint;
- alt-screen rendering / chrome persisting through a turn; resize/scroll during a
  turn; the ``SHELLPILOT_UI=app`` opt-in entry end-to-end (``run_app``).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from shellpilot.cli.app import build_app
from shellpilot.cli.app_turn import Scheduled, ThreadedUI, TurnRunner
from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.slash import command_words
from shellpilot.cli.theme import UNICODE_GLYPHS
from shellpilot.config.model import Settings
from shellpilot.llm.client import GenerationCancelled
from shellpilot.llm.messages import Message
from shellpilot.llm.ollama import OllamaResponseError
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.events import TurnStats
from tests.fakes.fake_llm import FakeLLM, answer, tool_call

# --- helpers ------------------------------------------------------------------


@dataclass
class _RecordingUI:
    """Records every UI call in order; optionally forwards to a real inner UI.

    Cross-method call order is captured (FakeUI keeps only per-method lists), so
    this proves both ordering and "marshaled, not inline". When ``forward`` is an
    ``AppUI``, the recorded calls also flow into the real render path.
    """

    forward: Any | None = None
    events: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def _record(self, name: str, *args: object) -> None:
        self.events.append((name, args))
        if self.forward is not None:
            getattr(self.forward, name)(*args)

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def stream_token(self, token: str) -> None:
        self._record("stream_token", token)

    def stream_thinking(self, text: str) -> None:
        self._record("stream_thinking", text)

    def begin_response(self) -> None:
        self._record("begin_response")

    def end_response(self) -> None:
        self._record("end_response")

    def turn_finished(self, stats: TurnStats) -> None:
        self._record("turn_finished", stats)

    def abort_turn(self) -> None:
        self._record("abort_turn")

    def fail_turn(self, message: str) -> None:
        self._record("fail_turn", message)

    def show_status(self, text: str) -> None:
        self._record("show_status", text)

    def show_error(self, text: str) -> None:
        self._record("show_error", text)

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        self._record("show_tool_call", name, arguments)

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._record("show_tool_result", name, success, summary)

    def show_command_output(self, line: str) -> None:
        self._record("show_command_output", line)

    def show_plan_progress(self, plan: object) -> None:
        self._record("show_plan_progress", plan)

    def ask_approval(self, request: object) -> object:
        raise NotImplementedError

    def ask_plan_approval(self, plan: object, path: str) -> tuple[str, str]:
        raise NotImplementedError


def _make_runtime(llm: Any, ui: Any, tmp_path: Path) -> ConversationRuntime:
    return ConversationRuntime(
        llm=llm,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=ui,
    )


_STATS = TurnStats(elapsed_s=1.0, context_tokens=1, context_pct=1, warn=False, output_tokens=1)

# (method name, positional args) for every fire-and-forget content method.
_FIRE_AND_FORGET: list[tuple[str, tuple[object, ...]]] = [
    ("stream_token", ("tok",)),
    ("stream_thinking", ("th",)),
    ("begin_response", ()),
    ("end_response", ()),
    ("turn_finished", (_STATS,)),
    ("show_status", ("st",)),
    ("show_error", ("er",)),
    ("show_tool_call", ("toolname", {"a": 1})),
    ("show_tool_result", ("toolname", True, "ok")),
    ("show_command_output", ("a line",)),
    ("show_plan_progress", (object(),)),
]


# --- ThreadedUI marshaling (single-threaded, synchronous schedule) ------------


@pytest.mark.parametrize("method,args", _FIRE_AND_FORGET, ids=[m for m, _ in _FIRE_AND_FORGET])
def test_fire_and_forget_marshals_not_inline(method: str, args: tuple[object, ...]) -> None:
    inner = _RecordingUI()
    sink: list[Scheduled] = []
    ui = ThreadedUI(inner=inner, schedule=sink.append)

    getattr(ui, method)(*args)

    # The call did NOT touch the inner UI yet — it enqueued exactly one callable.
    assert inner.events == []
    assert len(sink) == 1
    # Draining runs the inner call with the captured args.
    sink[0]()
    assert inner.events == [(method, args)]


def test_marshaled_calls_drain_in_fifo_order() -> None:
    inner = _RecordingUI()
    sink: list[Scheduled] = []
    ui = ThreadedUI(inner=inner, schedule=sink.append)

    ui.begin_response()
    ui.stream_token("a")
    ui.show_tool_call("read_file", {"path": "x"})
    ui.end_response()
    ui.turn_finished(_STATS)

    assert inner.events == []  # nothing ran inline
    for fn in sink:
        fn()
    assert inner.names() == [
        "begin_response",
        "stream_token",
        "show_tool_call",
        "end_response",
        "turn_finished",
    ]


def test_partial_captures_args_not_late_binding() -> None:
    # The classic closure bug: a loop variable would drain as ("b", "b"). partial
    # binds the arg at enqueue time, so this drains as ("a", "b").
    inner = _RecordingUI()
    sink: list[Scheduled] = []
    ui = ThreadedUI(inner=inner, schedule=sink.append)

    ui.stream_token("a")
    ui.stream_token("b")
    for fn in sink:
        fn()

    assert inner.events == [("stream_token", ("a",)), ("stream_token", ("b",))]


def test_blocking_methods_delegate_straight_to_inner() -> None:
    # ask_approval/ask_plan_approval return a value → cannot be fire-and-forget;
    # they run on the inner directly (the worker thread), which raises until
    # branch 7. They are never enqueued.
    inner = _RecordingUI()
    sink: list[Scheduled] = []
    ui = ThreadedUI(inner=inner, schedule=sink.append)

    with pytest.raises(NotImplementedError):
        ui.ask_approval(object())
    with pytest.raises(NotImplementedError):
        ui.ask_plan_approval(object(), "p")
    assert sink == []


# --- TurnRunner: a full turn across a real worker thread ----------------------


def test_full_turn_runs_on_worker_and_marshals(tmp_path: Path) -> None:
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    inner = _RecordingUI(forward=app_ui)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=inner, schedule=q.put)
    threaded = ThreadedUI(inner=inner, schedule=q.put)
    fake = FakeLLM(script=[answer("Hello from the worker thread.")])
    runtime = _make_runtime(fake, threaded, tmp_path)
    runner.conversation = runtime

    runner.start("hi")
    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()

    # Drain on the test thread — join established happens-before, so no race.
    while not q.empty():
        q.get()()

    # The response reached the REAL AppUI render (not just the recorder).
    assert "Hello from the worker thread." in app_ui._render_ansi()
    # Ordering: begin_response → stream_token(s) → end_response → turn_finished.
    names = inner.names()
    assert "stream_token" in names
    assert names[0] == "begin_response"
    assert names[-1] == "turn_finished"
    begin = names.index("begin_response")
    end = names.index("end_response")
    finished = names.index("turn_finished")
    assert begin < end < finished
    # busy was set on start and cleared by the scheduled _mark_done.
    assert runner._busy is False


def test_start_action_runs_on_worker_and_clears_busy(tmp_path: Path) -> None:
    # The /plan revise + /model list slash paths run via start_action. Prove the
    # fn runs on a REAL separate thread and busy is cleared only by the MARSHALED
    # _mark_done (not inline on the worker) — the real-threading ordering the
    # injected-fake router tests can't cover.
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=app_ui, schedule=q.put)

    ran = threading.Event()
    worker_ident: dict[str, int] = {}

    def action() -> None:
        worker_ident["id"] = threading.get_ident()
        ran.set()

    assert runner.start_action(action) is True
    assert runner._busy is True  # set synchronously in start_action
    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()
    assert ran.is_set()
    assert worker_ident["id"] != threading.get_ident()  # ran off the test thread
    # _mark_done was marshaled, not run inline — busy stays set until the queue drains.
    assert runner._busy is True
    while not q.empty():
        q.get()()
    assert runner._busy is False


def test_start_action_rejects_when_busy(tmp_path: Path) -> None:
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=app_ui, schedule=q.put)
    gate = threading.Event()

    assert runner.start_action(gate.wait) is True
    assert runner._busy is True
    # A second action is rejected (returns False) while the first is in flight.
    second_ran = threading.Event()
    assert runner.start_action(second_ran.set) is False
    assert not second_ran.is_set()
    gate.set()
    assert runner._thread is not None
    runner._thread.join(5.0)
    while not q.empty():
        q.get()()
    assert runner._busy is False


def test_start_action_failure_is_leak_free(tmp_path: Path) -> None:
    # A /plan revise (or other worker-routed slash command) failure must route
    # through describe_turn_error like _run does — never raw str(exc), which can
    # embed up to 200 chars of an upstream stream line (internal IPs / infra JSON).
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    inner = _RecordingUI(forward=app_ui)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=inner, schedule=q.put)

    def action() -> None:
        raise OllamaResponseError("unexpected stream chunk shape: SECRET-UPSTREAM-BODY")

    assert runner.start_action(action) is True
    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()

    while not q.empty():
        q.get()()

    errors = [args for name, args in inner.events if name == "show_error"]
    assert len(errors) == 1
    message = str(errors[0][0])
    assert "SECRET-UPSTREAM-BODY" not in message
    assert message == "Command failed: The model response was incomplete or malformed."
    assert runner._busy is False


def test_mark_done_fires_on_idle_after_busy_clears(tmp_path: Path) -> None:
    # The §31.18 queue fires at turn end via TurnRunner.on_idle, invoked by
    # _mark_done. Prove the WIRING (not just _fire_pending in isolation): on_idle
    # runs when the scheduled _mark_done drains, and only then — with busy already
    # cleared, so the fired follow-up turn sees idle.
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=app_ui, schedule=q.put)
    fired_busy: list[bool] = []
    runner.on_idle = lambda: fired_busy.append(runner.busy)  # record busy at fire time

    assert runner.start_action(lambda: None) is True
    assert runner._thread is not None
    runner._thread.join(5.0)
    # _mark_done is SCHEDULED, not yet drained → on_idle has not fired.
    assert fired_busy == []
    while not q.empty():
        q.get()()
    # Drained: on_idle fired exactly once, and saw busy already False.
    assert fired_busy == [False]
    assert runner.busy is False


def test_busy_guard_ignores_second_start(tmp_path: Path) -> None:
    gate = threading.Event()
    entered = threading.Event()
    chats = {"count": 0}
    inner_fake = FakeLLM(script=[answer("first"), answer("second")])

    class _GatedLLM:
        """Blocks the first chat until released, so the turn is genuinely in flight."""

        def chat(self, *args: Any, **kwargs: Any) -> Message:
            chats["count"] += 1
            entered.set()
            assert gate.wait(5.0)
            return inner_fake.chat(*args, **kwargs)

        def health(self) -> bool:
            return inner_fake.health()

        def list_models(self) -> Any:
            return inner_fake.list_models()

        def model_context_length(self, model: str) -> int | None:
            return inner_fake.model_context_length(model)

        def model_capabilities(self, model: str) -> tuple[str, ...]:
            return inner_fake.model_capabilities(model)

        def preload(self, model: str, *, keep_alive: str = "5m") -> None:
            inner_fake.preload(model, keep_alive=keep_alive)

    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=app_ui, schedule=q.put)
    threaded = ThreadedUI(inner=app_ui, schedule=q.put)
    runtime = _make_runtime(_GatedLLM(), threaded, tmp_path)
    runner.conversation = runtime

    runner.start("one")
    # busy is set synchronously in start(), before the worker makes progress.
    assert runner._busy is True
    assert entered.wait(5.0)  # the worker really entered the model call → in flight
    first_thread = runner._thread

    runner.start("two")  # must be IGNORED — a turn is already in flight
    assert runner._busy is True
    assert runner._thread is first_thread  # no second worker spawned

    gate.set()
    assert first_thread is not None
    first_thread.join(5.0)
    while not q.empty():
        q.get()()

    assert runner._busy is False  # _mark_done ran on drain
    assert chats["count"] == 1  # only ONE worker ever ran the model

    # busy reset → a fresh turn now runs (gate is already set, so it won't block).
    runner.start("three")
    assert runner._thread is not None and runner._thread is not first_thread
    runner._thread.join(5.0)
    while not q.empty():
        q.get()()
    assert chats["count"] == 2
    assert runner._busy is False


def test_worker_exception_is_surfaced_and_clears_busy(tmp_path: Path) -> None:
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    inner = _RecordingUI(forward=app_ui)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=inner, schedule=q.put)
    threaded = ThreadedUI(inner=inner, schedule=q.put)
    # An exhausted script makes the first chat() raise — the worker must not die
    # silently; the error must reach the pane and busy must clear.
    fake = FakeLLM(script=[])
    runtime = _make_runtime(fake, threaded, tmp_path)
    runner.conversation = runtime

    runner.start("boom")
    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()  # returned cleanly, not killed mid-stack

    while not q.empty():
        q.get()()

    # A turn failure routes through fail_turn (clears the dangling indicator),
    # NOT raw show_error, and the message is the friendly, leak-free description.
    fails = [args for name, args in inner.events if name == "fail_turn"]
    assert len(fails) == 1
    assert "show_error" not in inner.names()  # the raw "Turn failed: {exc}" path is gone
    assert "unexpectedly" in str(fails[0][0])  # FakeLLM raises a plain Exception
    assert runner._busy is False  # the finally ran


# --- Branch-6 turn cancellation (§31.15) --------------------------------------


class _CancellableLLM:
    """chat() blocks until released, then raises GenerationCancelled iff cancel is set.

    The same fake drives both paths: with the cancel event set it aborts (mirrors
    OllamaClient hitting the read boundary), and with it unset it returns a plain
    answer (a normal completion).  Non-chat protocol methods delegate to a FakeLLM.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._inner = FakeLLM(script=[])

    def chat(self, *args: Any, cancel: Any = None, **kwargs: Any) -> Message:
        self.entered.set()
        assert self.release.wait(5.0)
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled
        return answer("done")

    def health(self) -> bool:
        return self._inner.health()

    def list_models(self) -> Any:
        return self._inner.list_models()

    def model_context_length(self, model: str) -> int | None:
        return self._inner.model_context_length(model)

    def model_capabilities(self, model: str) -> tuple[str, ...]:
        return self._inner.model_capabilities(model)

    def preload(self, model: str, *, keep_alive: str = "5m") -> None:
        self._inner.preload(model, keep_alive=keep_alive)


def test_request_cancel_aborts_turn_cleanly_and_reruns(tmp_path: Path) -> None:
    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    inner = _RecordingUI(forward=app_ui)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=inner, schedule=q.put)
    threaded = ThreadedUI(inner=inner, schedule=q.put)
    llm = _CancellableLLM()
    runtime = _make_runtime(llm, threaded, tmp_path)
    runner.conversation = runtime

    # Idle: there is no turn, so request_cancel reports nothing to cancel.
    assert runner.request_cancel() is False

    runner.start("hi")
    assert llm.entered.wait(5.0)  # the worker really entered the model call → in flight
    assert runner._busy is True
    # Busy: request_cancel sets the event and reports True.
    assert runner.request_cancel() is True
    llm.release.set()  # let chat wake; it sees the set event and raises

    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()  # cancelled cleanly, not killed mid-stack
    while not q.empty():
        q.get()()

    names = inner.names()
    assert "abort_turn" in names  # the CLEAN abort path ran ...
    assert "show_error" not in names  # ... NOT the "Turn failed" error path
    assert "turn_finished" not in names  # the turn did not complete
    assert runner._busy is False  # _mark_done cleared busy on the cancel path
    assert runner.request_cancel() is False  # idle again
    assert "aborted" in app_ui._render_ansi()  # the marker reached the real render

    # busy reset → a fresh turn now runs to a normal completion after the cancel.
    llm.entered.clear()
    llm.release.clear()
    runner.start("again")
    assert llm.entered.wait(5.0)
    llm.release.set()  # no cancel this time → chat returns a normal answer
    runner._thread.join(5.0)
    while not q.empty():
        q.get()()
    assert "turn_finished" in inner.names()
    assert runner._busy is False


def test_request_cancel_kills_running_command_and_aborts(tmp_path: Path) -> None:
    """Branch 6b (§31.15): Ctrl-C during a running tool aborts cleanly and reruns.

    A blocking tool stands in for a long run_command child: its handler waits on
    the turn's cancel event, so request_cancel both releases it (a real kill in
    production) and trips the tool-loop abort — reaching abort_turn, never the
    failure path. A fresh turn then completes normally.
    """
    from shellpilot.llm.messages import ToolDefinition
    from shellpilot.policy.risk import RiskLevel, SideEffect
    from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec
    from shellpilot.tools.registry import ToolRegistry

    app_ui = AppUI(glyphs=UNICODE_GLYPHS, workspace=tmp_path, width_fn=lambda: 80)
    inner = _RecordingUI(forward=app_ui)
    q: queue.Queue[Scheduled] = queue.Queue()
    runner = TurnRunner(inner_ui=inner, schedule=q.put)
    threaded = ThreadedUI(inner=inner, schedule=q.put)

    entered = threading.Event()

    def _blocking_handler(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        entered.set()
        assert context.cancel is not None
        context.cancel.wait(5.0)  # blocks until request_cancel sets the turn's event
        return ToolResult(success=True, summary="unblocked", content="")

    spec = ToolSpec(
        definition=ToolDefinition(name="block_tool", description="d", parameters={}, required=()),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_blocking_handler,
    )
    registry = ToolRegistry()
    registry.register(spec)
    fake = FakeLLM(script=[tool_call("block_tool"), answer("a normal completion")])
    runtime = ConversationRuntime(
        llm=fake,
        settings=Settings(),
        workspace=tmp_path,
        behavior=BehaviorInstructions(global_text=None, project_text=None),
        ui=threaded,
        registry=registry,
    )
    runner.conversation = runtime

    runner.start("run something slow")
    assert entered.wait(5.0)  # the worker is inside the blocking tool → in flight
    assert runner._busy is True
    assert runner.request_cancel() is True  # sets the turn's cancel → tool unblocks

    assert runner._thread is not None
    runner._thread.join(5.0)
    assert not runner._thread.is_alive()  # aborted cleanly, not killed mid-stack
    while not q.empty():
        q.get()()

    names = inner.names()
    assert "abort_turn" in names  # the CLEAN abort path ran ...
    assert "show_error" not in names  # ... NOT the "Turn failed" error path
    assert "turn_finished" not in names  # the turn did not complete
    assert runner._busy is False
    assert "aborted" in app_ui._render_ansi()
    # Isolates THIS branch's tool-loop raise from branch-6's model-stream backstop:
    # without the raise the block_tool's "unblocked" result would be recorded and
    # the model re-invoked one round later (the backstop still aborts, but leaves
    # assistant+tool messages behind). The raise + history rollback leave only the
    # user message — no recorded tool result, no orphaned tool_call.
    assert [m.role for m in runtime._history] == ["user"]

    # A fresh turn completes normally after the cancel.
    runner.start("again")
    assert runner._thread is not None
    runner._thread.join(5.0)
    while not q.empty():
        q.get()()
    assert "turn_finished" in inner.names()
    assert runner._busy is False


# --- build_app on_submit wiring (headless, pipe input) ------------------------


def _headless_app(
    tmp_path: Path,
    inp: object,
    *,
    ui: AppUI,
    on_submit: Any | None = None,
    commands: Sequence[str] | None = None,
) -> Any:
    return build_app(
        workspace=tmp_path,
        model="gemma4:e4b",
        profile="balanced",
        glyphs=UNICODE_GLYPHS,
        commands=commands if commands is not None else command_words(),
        input=inp,  # type: ignore[arg-type]
        output=DummyOutput(),
        ui=ui,
        on_submit=on_submit,
    )


def test_build_app_on_submit_receives_text(tmp_path: Path) -> None:
    received: list[str] = []
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    with create_pipe_input() as inp:
        app = _headless_app(tmp_path, inp, ui=ui, on_submit=received.append)
        inp.send_text("hello there\n")
        inp.send_text("/exit\n")
        app.run()
    assert received == ["hello there"]
    # The submit echoes the user message into the pane (branch 5, §31.14) BEFORE
    # routing the text to on_submit, so the typed line is now visible.
    assert "hello there" in ui._render_ansi()


def test_build_app_on_submit_none_falls_back_to_echo(tmp_path: Path) -> None:
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    with create_pipe_input() as inp:
        app = _headless_app(tmp_path, inp, ui=ui, on_submit=None)
        inp.send_text("echoed line\n")
        inp.send_text("/exit\n")
        app.run()
    assert "echoed line" in ui._render_ansi()


def test_build_app_exit_is_never_routed_to_on_submit(tmp_path: Path) -> None:
    received: list[str] = []
    ui = AppUI(glyphs=UNICODE_GLYPHS, width_fn=lambda: 80)
    with create_pipe_input() as inp:
        app = _headless_app(tmp_path, inp, ui=ui, on_submit=received.append)
        inp.send_text("/exit\n")
        app.run()  # returns → /exit exited cleanly
    assert received == []
