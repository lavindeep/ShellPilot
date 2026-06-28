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

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output

from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.input import SlashCompleter
from shellpilot.cli.status_bar import status_bar
from shellpilot.cli.theme import (
    COLOR_ACCENT,
    COLOR_FAINT,
    UNICODE_GLYPHS,
    Glyphs,
)

# The dock grows to fit multi-line input up to this many rows, then scrolls
# internally. NOTE: a fixed cap — a per-terminal-height fraction would be nicer
# on a very short terminal, but a flat cap is enough until the live wiring
# (branch 4) shows whether it bites.
DOCK_MAX_ROWS = 10

# Idle Ctrl-C hint. NOTE: there is no turn to cancel in this inert shell; branch
# 6 replaces this with real turn cancellation / subprocess kill.
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


def _scroll_pane(window: Window, direction: int) -> None:
    """Scroll the (unfocused) chat pane one page, clamped to its content.

    The dock holds focus, so PageUp/PageDown and the wheel can't fall through to
    the pane on their own — this nudges the pane window's own vertical scroll
    using the public ``WindowRenderInfo`` heights, so wrapping is accounted for
    and we never scroll past the ends.
    """
    info = window.render_info
    if info is None:
        return
    page = max(1, info.window_height - 1)
    max_scroll = max(0, info.content_height - info.window_height)
    window.vertical_scroll = max(0, min(max_scroll, window.vertical_scroll + direction * page))


def build_app(
    *,
    workspace: Path,
    model: str,
    profile: str,
    glyphs: Glyphs,
    commands: Sequence[str],
    is_cloud: bool = False,
    ctx_pct: int = 0,
    input: Input | None = None,
    output: Output | None = None,
    ui: AppUI | None = None,
    on_submit: Callable[[str], None] | None = None,
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
        )
    # Explicit AppUI annotation (not AppUI | None) so the closures below capture a
    # non-optional type; ui is already narrowed to AppUI by the guard above.
    _ui: AppUI = ui
    pane_window = Window(
        FormattedTextControl(lambda: ANSI(_ui._render_ansi()), focusable=False),
        wrap_lines=False,
    )

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
        return list(
            status_bar(
                workspace=workspace,
                model=model,
                profile=profile,
                is_cloud=is_cloud,
                ctx_pct=ctx_pct,
                branch=branch,
                branch_glyph=branch_glyph,
            )
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

    kb = KeyBindings()

    # Enter submits. NOTE: a pipe sends LF (``\n`` → ``c-j``) and a real terminal
    # sends CR (``\r`` → ``enter``); both submit. A literal newline for multi-line
    # input is Alt+Enter (``escape, enter``), the prompt_toolkit convention.
    @kb.add("enter", filter=dock_focused)
    @kb.add("c-j", filter=dock_focused)
    def _submit(event: KeyPressEvent) -> None:
        text = dock_buffer.text
        if text.strip() == "/exit":
            event.app.exit()
            return
        if text.strip():
            if on_submit is not None:
                on_submit(text)
            else:
                _ui.show_status(text)
        dock_buffer.reset()

    @kb.add("escape", "enter", filter=dock_focused)
    def _newline(event: KeyPressEvent) -> None:
        dock_buffer.insert_text("\n")

    @kb.add("pageup")
    def _page_up(event: KeyPressEvent) -> None:
        _scroll_pane(pane_window, -1)

    @kb.add("pagedown")
    def _page_down(event: KeyPressEvent) -> None:
        _scroll_pane(pane_window, 1)

    @kb.add("c-c")
    def _interrupt(event: KeyPressEvent) -> None:
        _ui.show_status(_IDLE_HINT)

    # NOTE: mouse-wheel scroll over the pane needs no binding — prompt_toolkit
    # routes wheel events to the window under the cursor (the pane), whose own
    # ``_mouse_handler`` scrolls it, regardless of which window holds focus.
    return Application[None](
        layout=Layout(root, focused_element=dock_window),
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
    )
