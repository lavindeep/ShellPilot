"""Full-screen TUI app shell (UI v2 — design section 31).

The interactive REPL is moving from a per-turn ``prompt`` (which scrolls the
status bar and input away mid-turn) to a persistent full-screen
``prompt_toolkit`` ``Application`` on the alternate screen, so the status bar
and the input dock never vanish. This module is the **inert app shell**: it
builds the layout — a scrolling chat pane, a custom rounded multi-line input
dock, and the persistent status bar — with no runtime, model, or AI turn wired
in. Submitting echoes the dock text into the pane and clears the dock; ``/exit``
quits. Later branches wire the conversation, render Rich content in the pane,
add the thinking indicator, and handle Ctrl-C turn cancellation.

The dock border is drawn by hand (rounded corners, ASCII fallback) rather than
with ``prompt_toolkit``'s ``Frame``: ``Frame`` only draws square corners and
reserves completion-menu height inside the box, so it fills the terminal height
instead of hugging the input — this section is the one deliberate exception to
the §31.9 "borders come from rich primitives" contract, because the dock lives
in a ``prompt_toolkit`` layout, not a Rich render.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.input import SlashCompleter
from shellpilot.cli.render import _sanitize_line
from shellpilot.cli.status_bar import status_bar
from shellpilot.cli.theme import (
    COLOR_ACCENT,
    COLOR_FAINT,
    UNICODE_GLYPHS,
    Glyphs,
)

if TYPE_CHECKING:
    from shellpilot.cli.app_approval import ApprovalGate

# The dock grows to fit multi-line input up to this many rows, then scrolls
# internally. NOTE: a fixed cap — a per-terminal-height fraction would be nicer
# on a very short terminal, but a flat cap is enough until the live wiring
# (branch 4) shows whether it bites.
DOCK_MAX_ROWS = 10

# Idle Ctrl-C hint, shown only when no turn is in flight. Branch 6 (§31.15) wired
# real turn cancellation through on_interrupt; this hint is the idle fallback.
# NOTE: subprocess-kill on cancel is branch 6b.
_IDLE_HINT = "(idle — type /exit to quit)"


@dataclass(frozen=True)
class BoxChars:
    """Border glyphs for the rounded input dock; the ASCII set degrades them."""

    top_left: str
    top_right: str
    bottom_left: str
    bottom_right: str
    horizontal: str
    vertical: str


UNICODE_BOX = BoxChars("╭", "╮", "╰", "╯", "─", "│")
ASCII_BOX = BoxChars("+", "+", "+", "+", "-", "|")


def horizontal_border(width: int, box: BoxChars, *, top: bool) -> str:
    """A horizontal dock border line of exactly ``width`` cells.

    Pure function of ``width`` (and the glyph set): ``╭───╮`` for the top row,
    ``╰───╯`` for the bottom. The border, the pane wrap width, and the terminal
    width are one shared value (see :func:`build_app`), so this is rebuilt per
    render from the live terminal width and nothing caches a stale size.
    """
    if width < 2:
        return box.horizontal * max(0, width)
    left = box.top_left if top else box.bottom_left
    right = box.top_right if top else box.bottom_right
    return left + box.horizontal * (width - 2) + right


def _read_git_branch(workspace: Path) -> str | None:
    """Current git branch from ``<workspace>/.git/HEAD``, or None.

    Pure (one ``read_text``, no other I/O). Fails closed to None on every
    non-branch case: not a repo (no ``.git``), a worktree (``.git`` is a plain
    file → descending into it raises ``NotADirectoryError`` ⊂ ``OSError``),
    permission errors, and a detached HEAD (a bare SHA with no ``ref:`` prefix).
    """
    try:
        head = (workspace / ".git" / "HEAD").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # First line only + length cap: a real HEAD is one short ref line, but a
    # crafted clone's HEAD must not inject extra lines or unbounded text into the
    # status-bar dock that hosts the (unspoofable) cloud indicator.
    content = head.splitlines()[0].strip() if head else ""
    prefix = "ref: refs/heads/"
    if content.startswith(prefix):
        return content[len(prefix) :][:128] or None
    return None


def _scroll_up(scroll: int | None, last_line: int, page: int) -> int:
    """PageUp: move the pane's pinned cursor line up ``page`` lines.

    ``scroll`` is the currently pinned line, or None when following the bottom
    (then the cursor sits at ``last_line``). Returns the new pinned line — always
    an int, so PageUp leaves follow mode and the reader stays put as new output
    appends below.
    """
    current = last_line if scroll is None else scroll
    return max(0, current - page)


def _scroll_down(scroll: int | None, last_line: int, page: int) -> int | None:
    """PageDown: move the pane's pinned cursor line down ``page`` lines.

    Returns None once the cursor reaches the last line — i.e. scrolling back to
    the bottom resumes following (auto-scroll as the response streams in).
    """
    current = last_line if scroll is None else scroll
    new = min(last_line, current + page)
    return None if new >= last_line else new


@dataclass(frozen=True)
class StatusValues:
    """Live status-bar inputs, read per render so a mid-session ``/model use``,
    ``/profile use``, ``/cwd set``, or context growth reflects immediately
    (§31.18). ``branch`` is deliberately NOT here — it stays build-time (re-reading
    ``.git/HEAD`` every render would be wasteful)."""

    workspace: Path
    model: str
    profile: str
    is_cloud: bool
    ctx_pct: int


def build_app(
    *,
    workspace: Path,
    model: str,
    profile: str,
    glyphs: Glyphs,
    commands: Sequence[str],
    is_cloud: bool = False,
    ctx_pct: int = 0,
    show_reasoning: bool = True,
    input: Input | None = None,
    output: Output | None = None,
    ui: AppUI | None = None,
    on_submit: Callable[[str], None] | None = None,
    on_interrupt: Callable[[], bool] | None = None,
    on_slash: Callable[[str], None] | None = None,
    approval_gate: ApprovalGate | None = None,
    is_busy: Callable[[], bool] | None = None,
    register_idle: Callable[[Callable[[], None]], None] | None = None,
    status_fn: Callable[[], StatusValues] | None = None,
) -> Application[None]:
    """Build the full-screen app shell.

    ``input``/``output`` default to the real terminal; tests inject a pipe input
    and ``DummyOutput`` to drive the shell headlessly. ``on_submit`` receives the
    dock text on submit (branch 4 passes ``TurnRunner.start``); when it is None
    the shell falls back to the inert branch-3 echo so the standalone shell and
    its headless tests stay working. The non-TTY ``PlainInput`` path is untouched.
    """
    # One unicode/ascii decision drives the border, the branch glyph, and the
    # bar — recovered from the resolved glyph set the caller already probed
    # (``resolve_glyphs``), so we don't re-probe the console encoding here.
    unicode_mode = glyphs == UNICODE_GLYPHS
    box = UNICODE_BOX if unicode_mode else ASCII_BOX
    branch_glyph = "⎇" if unicode_mode else "git:"
    # The chip text already says "queued:", so the marker is a unicode-only
    # accent — no ASCII glyph (avoids a redundant "[queued] queued:").
    queued_marker = "⏳ " if unicode_mode else ""
    branch = _read_git_branch(workspace)

    # Chat pane: a FormattedTextControl rendering the AppUI transcript as Rich→ANSI.
    # Rich handles all wrapping at width W (wrap_lines=False on the Window), so the
    # ANSI string already contains the correct line breaks for the current width.
    # The AppUI re-renders when the width changes (cache miss in _render_ansi).
    if ui is None:
        ui = AppUI(
            glyphs=glyphs,
            workspace=workspace,
            width_fn=lambda: get_app().output.get_size().columns,
            show_reasoning=show_reasoning,
        )
    # Explicit AppUI annotation (not AppUI | None) so the closures below capture a
    # non-optional type; ui is already narrowed to AppUI by the guard above.
    _ui: AppUI = ui

    # Pane scroll state: "line" is the transcript line the pane keeps in view, or
    # None to follow the bottom (auto-scroll as a response streams in). A bare
    # FormattedTextControl has no cursor, so prompt_toolkit defaults it to (0,0)
    # and snaps the pane to the TOP every render — overriding any manual
    # vertical_scroll. Exposing this line as the UIContent cursor instead makes
    # pt scroll to keep it visible (independent of focus): following → bottom, and
    # a reader who paged up (a pinned line) is not yanked down when output appends.
    pane_scroll: dict[str, int | None] = {"line": None}

    # One-message queue (§31.18): a single staged line, set when a submit lands
    # while a turn is in flight, fired at turn end by _fire_pending. None = empty.
    # Loop-thread-only state, like pane_scroll.
    pending: dict[str, str | None] = {"text": None}

    def _pane_last_line() -> int:
        text = _ui._render_ansi()
        n = text.count("\n")
        # Rich ends each renderable with a newline, so the last real line is n-1.
        return max(0, n - 1) if text.endswith("\n") else n

    def _pane_cursor() -> Point:
        last = _pane_last_line()
        line = pane_scroll["line"]
        return Point(x=0, y=last if line is None else max(0, min(line, last)))

    class _PaneControl(FormattedTextControl):
        # Mouse-wheel scroll through the SAME cursor-line model as PageUp/PageDown
        # (§31.18): the Window's own vertical_scroll is overridden by the cursor
        # each render, so the wheel must be intercepted at the control and folded
        # into pane_scroll. Three lines per notch; returns None when handled.
        def mouse_handler(self, mouse_event: MouseEvent) -> object:
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                pane_scroll["line"] = _scroll_up(pane_scroll["line"], _pane_last_line(), 3)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                pane_scroll["line"] = _scroll_down(pane_scroll["line"], _pane_last_line(), 3)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                # Click on a diff/trail toggles its collapse state (§31.16/§31.19).
                # prompt_toolkit hands us the position in CONTENT coordinates (the
                # document row, scroll already accounted for), which is exactly the
                # transcript line toggle_at maps. Returning None when handled lets the
                # standard post-event redraw repaint (toggle_at cleared the cache).
                if _ui.toggle_at(mouse_event.position.y):
                    return None
            return super().mouse_handler(mouse_event)

    pane_window = Window(
        _PaneControl(
            lambda: ANSI(_ui._render_ansi()),
            focusable=False,
            show_cursor=False,
            get_cursor_position=_pane_cursor,
        ),
        wrap_lines=False,
    )

    def _pane_page() -> int:
        info = pane_window.render_info
        return max(1, (info.window_height - 1) if info is not None else 10)

    # Input dock: a focused, multi-line buffer with slash completion.
    dock_buffer = Buffer(
        name="dock",
        multiline=True,
        completer=SlashCompleter(commands),
        complete_while_typing=True,
    )
    dock_focused = has_focus(dock_buffer)

    def _dock_prefix(line_number: int, wrap_count: int) -> StyleAndTextTuples:
        if line_number == 0 and wrap_count == 0:
            return [(f"fg:{COLOR_ACCENT} bold", f"{glyphs.chevron} ")]
        return [("", "  ")]

    dock_window = Window(
        BufferControl(buffer=dock_buffer),
        height=Dimension(min=1, max=DOCK_MAX_ROWS),
        dont_extend_height=True,
        wrap_lines=True,
        get_line_prefix=_dock_prefix,
    )

    def _border(*, top: bool) -> Callable[[], StyleAndTextTuples]:
        # One shared width: the live terminal columns, read at render time so a
        # resize re-derives the line and nothing is pinned to a stale size.
        def _render() -> StyleAndTextTuples:
            width = get_app().output.get_size().columns
            return [(f"fg:{COLOR_FAINT}", horizontal_border(width, box, top=top))]

        return _render

    def _status() -> StyleAndTextTuples:
        # Live values (§31.18) when status_fn is wired — workspace/model/profile/
        # cloud/ctx re-read per render so /model use (the cloud indicator!),
        # /profile use, /cwd set, and context growth reflect immediately. The
        # cloud bit still comes from the real is_egressing signal (unspoofable).
        # branch stays build-time. Falls back to the static params (standalone
        # shell + existing tests) when status_fn is None.
        v = status_fn() if status_fn is not None else None
        return list(
            status_bar(
                workspace=v.workspace if v is not None else workspace,
                model=v.model if v is not None else model,
                profile=v.profile if v is not None else profile,
                is_cloud=v.is_cloud if v is not None else is_cloud,
                ctx_pct=v.ctx_pct if v is not None else ctx_pct,
                branch=branch,
                branch_glyph=branch_glyph,
            )
        )

    def _chip() -> StyleAndTextTuples:
        # A faint one-line "queued" chip above the dock border while a message is
        # staged (§31.18). The preview is user-controlled, so it is sanitized;
        # newlines collapse to spaces to keep the chip a single line, and it is
        # capped at ~60 cells. The glyph degrades to "[queued]" in ASCII mode.
        preview = _sanitize_line(pending["text"] or "").replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + glyphs.ellipsis
        return [(f"fg:{COLOR_FAINT}", f"{queued_marker}queued: {preview}")]

    chip_window = ConditionalContainer(
        content=Window(FormattedTextControl(_chip), height=1),
        filter=Condition(lambda: pending["text"] is not None),
    )

    def _bar() -> Window:
        return Window(width=1, char=box.vertical, style=f"fg:{COLOR_FAINT}")

    dock_row = VSplit(
        [
            _bar(),
            Window(width=1, char=" "),
            dock_window,
            Window(width=1, char=" "),
            _bar(),
        ]
    )

    body = HSplit(
        [
            pane_window,
            chip_window,
            Window(FormattedTextControl(_border(top=True)), height=1),
            dock_row,
            Window(FormattedTextControl(_border(top=False)), height=1),
            Window(FormattedTextControl(_status), height=1),
        ]
    )
    root = FloatContainer(
        content=body,
        floats=[Float(content=CompletionsMenu(max_height=8, scroll_offset=1))],
    )

    def _dispatch_line(text: str) -> None:
        # The routing a non-staged submit takes (§31.18): slash/`!` → on_slash,
        # a normal line → echo + on_submit (or the inert show_status fallback).
        # A new turn jumps the pane back to the bottom to watch it stream.
        pane_scroll["line"] = None
        stripped = text.strip()
        if on_slash is not None and stripped and stripped[0] in "/!":
            # A typed slash or `!` line is a harness control, not a model turn:
            # route it to the SlashRouter (capture / run_in_terminal / manual
            # shell / exit), §31.17.
            on_slash(text)
            return
        if on_submit is not None:
            # Echo the typed line into the pane on the loop thread, then run the
            # turn. The live indicator renders just below this echo (§31.14).
            _ui.show_user_message(text)
            on_submit(text)
        else:
            _ui.show_status(text)

    def _fire_pending() -> None:
        # Loop-thread idle callback (TurnRunner.on_idle): fire the staged line, if
        # any, as a fresh turn. pending is cleared FIRST, so the new turn's own end
        # fires _fire_pending again against an empty slot — no loop (§31.18).
        if pending["text"] is None:
            return
        text = pending["text"]
        pending["text"] = None
        _dispatch_line(text)

    if register_idle is not None:
        register_idle(_fire_pending)

    kb = KeyBindings()

    # Enter submits. NOTE: a pipe sends LF (``\n`` → ``c-j``) and a real terminal
    # sends CR (``\r`` → ``enter``); both submit. A literal newline for multi-line
    # input is Alt+Enter (``escape, enter``), the prompt_toolkit convention.
    @kb.add("enter", filter=dock_focused)
    @kb.add("c-j", filter=dock_focused)
    def _submit(event: KeyPressEvent) -> None:
        # During an approval the dock IS the approval input: route the line to the
        # gate (which resolves the worker's Future) BEFORE the /exit check, so a
        # mid-approval "/exit" is an approval answer, not a quit (§31.16).
        if approval_gate is not None and approval_gate.active:
            line = dock_buffer.text
            dock_buffer.reset()
            approval_gate.submit(line)
            return
        text = dock_buffer.text
        dock_buffer.reset()
        if text.strip() == "/exit":
            event.app.exit()
            return
        if not text.strip():
            return
        if is_busy is not None and is_busy():
            # A submit while a turn is in flight is STAGED, not dropped (§31.18):
            # one slot, so a second submit replaces the first. It fires at turn
            # end via _fire_pending (TurnRunner.on_idle).
            pending["text"] = text
            return
        _dispatch_line(text)

    @kb.add(
        "up",
        filter=dock_focused
        & Condition(lambda: not dock_buffer.text and pending["text"] is not None),
    )
    def _recall(event: KeyPressEvent) -> None:
        # Up in an EMPTY dock with a staged message pulls it back into the box to
        # edit/clear/re-send; the chip disappears (pending cleared), §31.18. When
        # the box is non-empty or nothing is staged the filter is false and the
        # default Up (cursor up / history) applies — this binding is not reached.
        dock_buffer.text = pending["text"] or ""
        dock_buffer.cursor_position = len(dock_buffer.text)
        pending["text"] = None

    @kb.add(
        "c-o",
        filter=dock_focused & Condition(lambda: approval_gate is None or not approval_gate.active),
    )
    def _toggle_diff(event: KeyPressEvent) -> None:
        # Ctrl-O toggles the LATEST diff's collapse state (§31.16) — the keyboard
        # fallback for terminals without mouse reporting, where the per-element
        # click toggle is unavailable. A modifier key (not a bare letter) so it
        # never collides with typing a message; overrides prompt_toolkit's default
        # c-o the same way the app's c-c/c-d bindings override theirs. No-op when
        # there is no diff yet, so a stray press is harmless. Diffs and trails are
        # both reachable by CLICK; this fallback intentionally covers diffs only.
        _ui.toggle_latest_diff()

    @kb.add("escape", "enter", filter=dock_focused)
    def _newline(event: KeyPressEvent) -> None:
        dock_buffer.insert_text("\n")

    @kb.add("pageup")
    def _page_up(event: KeyPressEvent) -> None:
        pane_scroll["line"] = _scroll_up(pane_scroll["line"], _pane_last_line(), _pane_page())

    @kb.add("pagedown")
    def _page_down(event: KeyPressEvent) -> None:
        pane_scroll["line"] = _scroll_down(pane_scroll["line"], _pane_last_line(), _pane_page())

    @kb.add("c-c")
    def _interrupt(event: KeyPressEvent) -> None:
        # During an approval the worker is blocked on the gate's Future, not in a
        # model-stream read, so on_interrupt (model cancel) would not fire. Ctrl-C
        # must resolve the approval as a decline of THIS action; the turn continues
        # and turn-level cancel is available again after the prompt returns (mirrors
        # TerminalUI.ask_approval's KeyboardInterrupt → DECLINE) (§31.16).
        if approval_gate is not None and approval_gate.active:
            approval_gate.cancel()
            return
        # Raw mode disables ISIG, so Ctrl-C is a normal key press, not a SIGINT
        # (branch 6, §31.15). When a turn is in flight, on_interrupt cancels it and
        # returns True (the worker aborts the stream and renders the marker), so we
        # show nothing. Otherwise it is idle — fall back to the idle hint.
        if on_interrupt is not None and on_interrupt():
            # "Stop everything": a Ctrl-C that aborts the turn also DRAINS any
            # staged message, so a queued follow-up does not fire after the abort
            # (§31.18). Cleared here on the loop thread BEFORE the worker's
            # _mark_done schedules on_idle, so _fire_pending sees an empty slot.
            pending["text"] = None
            return
        _ui.show_status(_IDLE_HINT)

    @kb.add("c-d", filter=dock_focused)
    def _eof(event: KeyPressEvent) -> None:
        # EOF during an approval declines THIS action (same as Ctrl-C). Otherwise a
        # no-op: the dock owns c-d so a stray press never tears down the app.
        if approval_gate is not None and approval_gate.active:
            approval_gate.cancel()

    # NOTE: keyboard PageUp/PageDown and mouse-wheel scroll both drive the pane
    # via the cursor-line model — PageUp/PageDown through the keybindings above,
    # the wheel through _PaneControl.mouse_handler (§31.18), since the Window's
    # own vertical_scroll is re-derived from the cursor each render.
    return Application[None](
        layout=Layout(root, focused_element=dock_window),
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
        # Periodic repaint so the live thinking indicator's timer ticks and the
        # plane glides even between thinking chunks (design section 31.14). When
        # idle the per-tick AppUI._render_ansi is a width-cache hit (cheap no-op).
        # NOTE: gating the refresh on an active turn is the upgrade path if
        # profiling ever shows the idle tick costs anything (branch 5).
        refresh_interval=0.1,
        input=input,
        output=output,
    )
