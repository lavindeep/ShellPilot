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
from typing import TYPE_CHECKING, TypedDict

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.model_completion import ModelCompletionMatch, model_completion_matches
from shellpilot.cli.path_completion import PathCompletionMatch, path_completion_matches
from shellpilot.cli.render import _sanitize_line
from shellpilot.cli.slash import (
    SlashMenuItem,
    slash_menu_items,
    slash_menu_matches,
    slash_menu_open,
    slash_menu_window,
)
from shellpilot.cli.status_bar import status_bar
from shellpilot.cli.theme import (
    COLOR_ACCENT,
    COLOR_ERROR,
    COLOR_FAINT,
    COLOR_WARN,
    UNICODE_GLYPHS,
    Glyphs,
)
from shellpilot.policy.risk import RiskLevel

if TYPE_CHECKING:
    from shellpilot.cli.app_approval import ApprovalGate
    from shellpilot.llm.ollama import LocalModel

# The dock grows to fit multi-line input up to this many rows, then scrolls
# internally. NOTE: a fixed cap — a per-terminal-height fraction would be nicer
# on a very short terminal, but a flat cap is enough until the live wiring
# (branch 4) shows whether it bites.
DOCK_MAX_ROWS = 10

# The slash menu (§31.20) shows this many command rows at once; ↑/↓ scroll the
# window through a longer filtered list.
MENU_VISIBLE_ROWS = 3

# Idle Ctrl-C hint, shown only when no turn is in flight. Branch 6 (§31.15) wired
# real turn cancellation through on_interrupt; this hint is the idle fallback.
# NOTE: subprocess-kill on cancel is branch 6b.
_IDLE_HINT = "(idle — type /exit to quit)"


class _SlashMenuState(TypedDict):
    index: int
    query: str
    preview: bool
    suppress_change: bool


class _PathMenuState(TypedDict):
    index: int


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


def horizontal_border(width: int, box: BoxChars, *, top: bool, label: str | None = None) -> str:
    """A horizontal dock border line of exactly ``width`` cells.

    Pure function of ``width`` (and the glyph set): ``╭───╮`` for the top row,
    ``╰───╯`` for the bottom. The border, the pane wrap width, and the terminal
    width are one shared value (see :func:`build_app`), so this is rebuilt per
    render from the live terminal width and nothing caches a stale size.

    ``label`` (modal dock, §31.16) embeds a short state hint into the TOP
    border — ``╭─ approve? ───╮`` — so the dock says what it is asking while an
    approval owns the input. Dropped whole when it cannot fit the width; the
    bottom border never carries it.
    """
    if width < 2:
        return box.horizontal * max(0, width)
    left = box.top_left if top else box.bottom_left
    right = box.top_right if top else box.bottom_right
    if top and label:
        decorated = f"{box.horizontal} {label} "
        if len(decorated) + 2 <= width:
            fill = box.horizontal * (width - 2 - len(decorated))
            return left + decorated + fill + right
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


def _branch_resolver(
    initial_workspace: Path, initial_branch: str | None
) -> Callable[[Path], str | None]:
    """Map the live workspace to its git branch, re-reading ``.git/HEAD`` ONLY when
    the workspace changes — a ``/cwd`` — never per render (§31.18).

    Seeded with the build-time ``(workspace, branch)`` so the first frames cost no
    I/O. When the status bar's live workspace differs from the last one resolved,
    the branch is re-read (``None`` when the new directory is not a repo) and
    cached, so the status-bar segment follows cwd into and out of a repo without an
    ``.git`` read on every repaint.
    """
    last_workspace = initial_workspace
    branch = initial_branch

    def resolve(workspace: Path) -> str | None:
        nonlocal last_workspace, branch
        if workspace != last_workspace:
            last_workspace = workspace
            branch = _read_git_branch(workspace)
        return branch

    return resolve


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
    (§31.18). ``branch`` is deliberately NOT a field — it is derived from
    ``workspace`` by ``_branch_resolver``, which re-reads ``.git/HEAD`` only when
    the workspace changes, so the segment follows ``/cwd`` without an ``.git`` read
    on every repaint."""

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
    model_completion_models: Callable[[], list[LocalModel]] | None = None,
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
    # The branch segment follows the live workspace (a /cwd) but re-reads .git/HEAD
    # only when the workspace changes, not per render (§31.18).
    resolve_branch = _branch_resolver(workspace, _read_git_branch(workspace))

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

    # Pane render cache (perf): _render_ansi already returns the SAME string object
    # while the transcript content and width are unchanged (its width cache), so we
    # parse the ANSI and count its lines ONLY when that string actually changes — an
    # identity compare, O(1) on a scroll or idle-refresh tick. Constructing ANSI(...)
    # re-parses the whole transcript (ANSI parses in __init__) and prompt_toolkit's
    # FormattedTextControl cache is keyed by the per-render counter, so without this
    # every redraw re-parsed the entire transcript — the source of the scroll lag.
    pane_render: dict[str, tuple[str, ANSI, int]] = {}

    def _pane_view() -> tuple[ANSI, int]:
        text = _ui._render_ansi()
        cached = pane_render.get("v")
        if cached is None or cached[0] is not text:
            n = text.count("\n")
            # Rich ends each renderable with a newline, so the last real line is n-1.
            last = max(0, n - 1) if text.endswith("\n") else n
            cached = (text, ANSI(text), last)
            pane_render["v"] = cached
        return cached[1], cached[2]

    def _pane_last_line() -> int:
        return _pane_view()[1]

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
            lambda: _pane_view()[0],
            focusable=False,
            show_cursor=False,
            get_cursor_position=_pane_cursor,
        ),
        wrap_lines=False,
    )

    def _pane_page() -> int:
        info = pane_window.render_info
        return max(1, (info.window_height - 1) if info is not None else 10)

    # Input dock: a focused, multi-line buffer. Slash completion is the custom
    # in-app menu below (§31.20), NOT prompt_toolkit's completer/CompletionsMenu —
    # so there is no `completer` here and Tab is free for the menu's fill binding.
    dock_buffer = Buffer(name="dock", multiline=True)
    dock_focused = has_focus(dock_buffer)

    # The slash menu (§31.20): rows derived once from HELP_ROWS; `index` is the
    # current selection. `query` is the user-typed slash token used for filtering;
    # arrow-key previews can write multi-word commands into the dock while this
    # query keeps the menu open over the original sibling set.
    menu_items = slash_menu_items()
    menu_label_width = max((len(it.label) for it in menu_items), default=0)
    slash_menu: _SlashMenuState = {
        "index": 0,
        "query": "",
        "preview": False,
        "suppress_change": False,
    }
    path_menu: _PathMenuState = {"index": 0}

    def _clear_menu_state() -> None:
        slash_menu["index"] = 0
        slash_menu["query"] = ""
        slash_menu["preview"] = False
        slash_menu["suppress_change"] = False
        path_menu["index"] = 0

    def _reset_menu_index(_buffer: Buffer) -> None:
        if slash_menu["suppress_change"]:
            return
        slash_menu["index"] = 0
        slash_menu["query"] = dock_buffer.text
        slash_menu["preview"] = False
        path_menu["index"] = 0

    dock_buffer.on_text_changed += _reset_menu_index

    def _current_workspace() -> Path:
        values = status_fn() if status_fn is not None else None
        return values.workspace if values is not None else workspace

    def _menu_query() -> str:
        if slash_menu["preview"]:
            return str(slash_menu["query"])
        return dock_buffer.text

    def _menu_matches() -> list[SlashMenuItem]:
        return slash_menu_matches(_menu_query(), menu_items)

    def _menu_open() -> bool:
        # Closed during an approval (the dock is the approval input then) and when
        # nothing matches; otherwise open while the command token is being typed.
        if approval_gate is not None and approval_gate.active:
            return False
        return slash_menu_open(_menu_query()) and bool(_menu_matches())

    def _menu_index() -> int:
        matches = _menu_matches()
        if not matches:
            return 0
        return max(0, min(slash_menu["index"], len(matches) - 1))

    menu_open = Condition(_menu_open)

    def _path_matches() -> list[PathCompletionMatch | ModelCompletionMatch]:
        path_matches: list[PathCompletionMatch | ModelCompletionMatch] = list(
            path_completion_matches(
                dock_buffer.document.text_before_cursor,
                _current_workspace(),
            )
        )
        if path_matches:
            return path_matches
        if model_completion_models is None:
            return []
        return list(
            model_completion_matches(
                dock_buffer.document.text_before_cursor,
                model_completion_models(),
            )
        )

    def _path_menu_open() -> bool:
        if approval_gate is not None and approval_gate.active:
            return False
        return not _menu_open() and bool(_path_matches())

    def _path_menu_index() -> int:
        matches = _path_matches()
        if not matches:
            return 0
        return max(0, min(path_menu["index"], len(matches) - 1))

    path_menu_open = Condition(_path_menu_open)

    def _dock_color() -> str:
        # The modal dock (§31.16): during an approval the border carries the
        # decision color — red for a HIGH-risk prompt, amber for any other —
        # matching the approval card in the pane; idle it stays faint. Read
        # per render, so the swap tracks the gate exactly.
        if approval_gate is not None and approval_gate.active:
            return COLOR_ERROR if approval_gate.dock_risk is RiskLevel.HIGH else COLOR_WARN
        return COLOR_FAINT

    def _dock_label() -> str | None:
        if approval_gate is not None and approval_gate.active:
            return approval_gate.dock_hint
        return None

    def _dock_prefix(line_number: int, wrap_count: int) -> StyleAndTextTuples:
        if line_number == 0 and wrap_count == 0:
            # The chevron follows the mode: the approval color while a prompt
            # owns the dock, accent green otherwise. Deliberately NOT keyed on
            # is_busy — a render must never consume a busy read the submit
            # keybinding's stage-or-dispatch decision depends on.
            if approval_gate is not None and approval_gate.active:
                color = _dock_color()
            else:
                color = COLOR_ACCENT
            return [(f"fg:{color} bold", f"{glyphs.chevron} ")]
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
        # resize re-derives the line and nothing is pinned to a stale size. The
        # color and the top-border label follow the approval state per render
        # (modal dock, §31.16).
        def _render() -> StyleAndTextTuples:
            width = get_app().output.get_size().columns
            label = _dock_label() if top else None
            return [(f"fg:{_dock_color()}", horizontal_border(width, box, top=top, label=label))]

        return _render

    def _status() -> StyleAndTextTuples:
        # Live values (§31.18) when status_fn is wired — workspace/model/profile/
        # cloud/ctx re-read per render so /model use (the cloud indicator!),
        # /profile use, /cwd set, and context growth reflect immediately. The
        # branch segment follows the live workspace via resolve_branch, which
        # re-reads .git/HEAD only when the workspace changes (a /cwd) — not per
        # render. The cloud bit still comes from the real is_egressing signal
        # (unspoofable). Falls back to the static params (standalone shell +
        # existing tests) when status_fn is None.
        v = status_fn() if status_fn is not None else None
        ws = v.workspace if v is not None else workspace
        return list(
            status_bar(
                workspace=ws,
                model=v.model if v is not None else model,
                profile=v.profile if v is not None else profile,
                is_cloud=v.is_cloud if v is not None else is_cloud,
                ctx_pct=v.ctx_pct if v is not None else ctx_pct,
                branch=resolve_branch(ws),
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

    def _menu_content() -> StyleAndTextTuples:
        # The slash menu rows (§31.20): a MENU_VISIBLE_ROWS window over the filtered
        # matches, the selected row carried in accent, others dim — higher contrast
        # than the default completion popup. Command labels (with their <arg>
        # placeholders) are padded to one column so descriptions align.
        matches = _menu_matches()
        if not matches:
            return []
        index = _menu_index()
        start = slash_menu_window(index, len(matches), MENU_VISIBLE_ROWS)
        caret_on = "▸ " if glyphs is UNICODE_GLYPHS else "> "
        frags: StyleAndTextTuples = []
        for offset, item in enumerate(matches[start : start + MENU_VISIBLE_ROWS]):
            selected = (start + offset) == index
            caret = caret_on if selected else "  "
            label_style = f"fg:{COLOR_ACCENT} bold" if selected else ""
            desc_style = f"fg:{COLOR_ACCENT}" if selected else f"fg:{COLOR_FAINT}"
            frags.append((label_style, f" {caret}{item.label}".ljust(menu_label_width + 4)))
            frags.append((desc_style, f"  {item.description}"))
            frags.append(("", "\n"))
        frags.pop()  # drop the trailing newline so the window height is exact
        return frags

    menu_window = ConditionalContainer(
        content=Window(
            FormattedTextControl(_menu_content),
            height=Dimension(max=MENU_VISIBLE_ROWS),
            dont_extend_height=True,
        ),
        filter=menu_open,
    )

    def _path_menu_content() -> StyleAndTextTuples:
        matches = _path_matches()
        if not matches:
            return []
        index = _path_menu_index()
        start = slash_menu_window(index, len(matches), MENU_VISIBLE_ROWS)
        caret_on = "▸ " if glyphs is UNICODE_GLYPHS else "> "
        frags: StyleAndTextTuples = []
        for offset, item in enumerate(matches[start : start + MENU_VISIBLE_ROWS]):
            selected = (start + offset) == index
            caret = caret_on if selected else "  "
            label_style = f"fg:{COLOR_ACCENT} bold" if selected else ""
            hint_style = f"fg:{COLOR_ACCENT}" if selected else f"fg:{COLOR_FAINT}"
            kind = (
                item.hint
                if isinstance(item, ModelCompletionMatch)
                else ("dir" if item.label.endswith("/") else "file")
            )
            frags.append((label_style, f" {caret}{item.label}"))
            frags.append((hint_style, f"  {kind}"))
            frags.append(("", "\n"))
        frags.pop()
        return frags

    path_menu_window = ConditionalContainer(
        content=Window(
            FormattedTextControl(_path_menu_content),
            height=Dimension(max=MENU_VISIBLE_ROWS),
            dont_extend_height=True,
        ),
        filter=path_menu_open,
    )

    def _bar() -> Window:
        # The side bars share the border's approval-state color (callable style,
        # re-evaluated per render).
        return Window(width=1, char=box.vertical, style=lambda: f"fg:{_dock_color()}")

    dock_row = VSplit(
        [
            _bar(),
            Window(width=1, char=" "),
            dock_window,
            Window(width=1, char=" "),
            _bar(),
        ]
    )

    root = HSplit(
        [
            pane_window,
            chip_window,
            menu_window,
            path_menu_window,
            Window(FormattedTextControl(_border(top=True)), height=1),
            dock_row,
            Window(FormattedTextControl(_border(top=False)), height=1),
            Window(FormattedTextControl(_status), height=1),
        ]
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
    def _submit_current() -> None:
        # The submit effect, callable from the Enter binding AND the slash menu's
        # smart-Enter on an argless command (which sets the dock text first).
        # During an approval the dock IS the approval input: route the line to the
        # gate (which resolves the worker's Future) BEFORE the /exit check, so a
        # mid-approval "/exit" is an approval answer, not a quit (§31.16).
        if approval_gate is not None and approval_gate.active:
            line = dock_buffer.text
            dock_buffer.reset()
            _clear_menu_state()
            approval_gate.submit(line)
            return
        text = dock_buffer.text
        dock_buffer.reset()
        _clear_menu_state()
        if text.strip() == "/exit":
            get_app().exit()
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

    @kb.add("enter", filter=dock_focused)
    @kb.add("c-j", filter=dock_focused)
    def _submit(event: KeyPressEvent) -> None:
        _submit_current()

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

    # Slash-menu navigation (§31.20). Registered AFTER _submit/_recall so that when
    # the menu is open these win the shared keys (last matching binding wins): ↑/↓
    # move the selection (the _recall ↑ filter is false here — its dock is empty,
    # this one's starts with '/'), Enter is smart, Tab fills. All gated on menu_open
    # so a normal message types literally.
    def _menu_fill(item: SlashMenuItem) -> None:
        # Put the command (no <arg> placeholders) in the box + a trailing space; the
        # space ends the token so the menu closes and the user types args (or runs).
        dock_buffer.text = item.fill + " "
        dock_buffer.cursor_position = len(dock_buffer.text)

    def _menu_preview(item: SlashMenuItem) -> None:
        # Arrow selection previews the highlighted command without accepting it.
        # The original typed query remains active so multi-word previews such as
        # "/model list" do not close the menu before the user can keep arrowing.
        slash_menu["suppress_change"] = True
        try:
            dock_buffer.text = item.fill
            dock_buffer.cursor_position = len(dock_buffer.text)
        finally:
            slash_menu["suppress_change"] = False
        slash_menu["preview"] = True

    @kb.add("up", filter=dock_focused & menu_open)
    def _menu_up(event: KeyPressEvent) -> None:
        matches = _menu_matches()
        slash_menu["index"] = max(0, _menu_index() - 1)
        _menu_preview(matches[_menu_index()])

    @kb.add("down", filter=dock_focused & menu_open)
    def _menu_down(event: KeyPressEvent) -> None:
        matches = _menu_matches()
        slash_menu["index"] = min(len(matches) - 1, _menu_index() + 1)
        _menu_preview(matches[_menu_index()])

    @kb.add("enter", filter=dock_focused & menu_open)
    def _menu_enter(event: KeyPressEvent) -> None:
        # Smart Enter: an argless command runs now; an arg command fills and waits.
        item = _menu_matches()[_menu_index()]
        if item.takes_args:
            _menu_fill(item)
        else:
            dock_buffer.text = item.fill
            _submit_current()

    @kb.add("tab", filter=dock_focused & menu_open)
    def _menu_tab(event: KeyPressEvent) -> None:
        _menu_fill(_menu_matches()[_menu_index()])

    @kb.add("up", filter=dock_focused & path_menu_open)
    def _path_menu_up(event: KeyPressEvent) -> None:
        matches = _path_matches()
        path_menu["index"] = max(0, _path_menu_index() - 1)
        if path_menu["index"] >= len(matches):
            path_menu["index"] = max(0, len(matches) - 1)

    @kb.add("down", filter=dock_focused & path_menu_open)
    def _path_menu_down(event: KeyPressEvent) -> None:
        matches = _path_matches()
        path_menu["index"] = min(len(matches) - 1, _path_menu_index() + 1)

    @kb.add("tab", filter=dock_focused & path_menu_open)
    def _path_menu_tab(event: KeyPressEvent) -> None:
        matches = _path_matches()
        if not matches:
            return
        fill = matches[_path_menu_index()].fill
        suffix = dock_buffer.document.text_after_cursor
        dock_buffer.text = fill + suffix
        dock_buffer.cursor_position = len(fill)

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
        # Deduped so repeated Ctrl-C while idle shows the hint once, not a stack.
        _ui.show_idle_hint(_IDLE_HINT)

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
        # No built-in refresh_interval (perf, §31.14): it starts ONE background task
        # that invalidates on a fixed timer forever, redrawing the static transcript
        # even when idle — wasted CPU the old REPL never spent. The live thinking
        # indicator is instead animated by run_app's gated refresh loop, which
        # invalidates ONLY while a turn is in flight (AppUI.is_animating); idle stays
        # purely event-driven (a redraw happens on a keystroke or scroll, not a timer).
        input=input,
        output=output,
    )
