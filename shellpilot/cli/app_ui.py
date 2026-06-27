"""AppUI: RuntimeUI implementation that routes content into the full-screen app pane.

This is the content seam for UI v2 (design section 31.12). ``AppUI`` holds a list
of Rich renderables as the transcript source of truth, renders them to a single
ANSI string at the shared terminal width, and caches the result until the content
or the width changes. The pane control in ``app.py`` is a ``FormattedTextControl``
over ``ANSI(ui._render_ansi())``, so Rich handles all wrapping and theming at width
W while prompt_toolkit handles layout and scrolling.

``AppUI`` is a pure state-and-render object: it never calls ``get_app()`` or
``invalidate()`` — those are branch-4 concerns — so it is fully testable without a
running app.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from shellpilot.cli.render import _sanitize_line, plan_step_line
from shellpilot.cli.render import tool_call as render_tool_call
from shellpilot.cli.render import tool_result as render_tool_result
from shellpilot.cli.theme import SHELLPILOT_THEME, UNICODE_GLYPHS, Glyphs
from shellpilot.memory.redaction import redact_structure
from shellpilot.tools.base import workspace_display

if TYPE_CHECKING:
    from shellpilot.policy.approvals import ApprovalReply, ApprovalRequest
    from shellpilot.runtime.events import TurnStats
    from shellpilot.runtime.planner import TaskPlan


class AppUI:
    """RuntimeUI implementation that routes content into the full-screen app pane.

    All methods must be called from the UI thread. Cross-thread marshaling and
    ``app.invalidate()`` are branch-4 concerns; this class stays pure state+render.
    """

    def __init__(
        self,
        *,
        glyphs: Glyphs = UNICODE_GLYPHS,
        workspace: Path | None = None,
        width_fn: Callable[[], int],
    ) -> None:
        self._glyphs = glyphs
        # Workspace for display-integrity (design section 14.5): when set, a
        # `path` argument in the tool-call line is shown as its resolved,
        # workspace-relative target — the SAME resolution the tool acts on.
        self._workspace = workspace
        self._width_fn = width_fn
        # Source of truth: every committed renderable in the transcript.
        self._renderables: list[RenderableType] = []
        # Accumulated token text for the current open response.
        # None means no response is open; an open response is the last renderable
        # in spirit but lives here until end_response() finalizes it.
        self._open_response: str | None = None
        # Width-keyed ANSI cache: (width, ansi_string), or None when stale.
        self._cache: tuple[int, str] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close_open_response(self) -> None:
        """Finalize the open response as a Markdown renderable, if one is open."""
        if self._open_response is not None:
            self._renderables.append(Markdown(_sanitize_line(self._open_response)))
            self._open_response = None
            self._cache = None

    def _add_renderable(self, renderable: RenderableType) -> None:
        """Close any open response first, then append a renderable to the transcript.

        Preserves ordering: response → tool call → response produces three distinct
        transcript entries (the open response closes around the tool call).
        """
        self._close_open_response()
        self._renderables.append(renderable)
        self._cache = None

    def _render_ansi(self) -> str:
        """Render the full transcript to an ANSI string at the current terminal width.

        The width is read from ``width_fn`` on every call and compared against the
        cache key; a resize automatically re-derives the ANSI (Rich re-wraps at the
        new width) and updates the cache. ``AppUI`` never calls ``get_app()``; the
        caller supplies the width source via ``width_fn`` at construction time.
        """
        width = self._width_fn()
        if self._cache is not None and self._cache[0] == width:
            return self._cache[1]
        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            theme=SHELLPILOT_THEME,
            width=width,
        )
        renderables: list[RenderableType] = list(self._renderables)
        if self._open_response is not None:
            # Include in-progress response without committing it yet. Sanitize the
            # model text at the sink (mirrors ResponseStream, streaming.py:171) —
            # _sanitize_line keeps LF, so markdown structure survives.
            renderables.append(Markdown(_sanitize_line(self._open_response)))
        for renderable in renderables:
            console.print(renderable)
        ansi = buf.getvalue()
        self._cache = (width, ansi)
        return ansi

    # ------------------------------------------------------------------
    # RuntimeUI content methods — mirroring TerminalUI exactly
    # ------------------------------------------------------------------

    def stream_token(self, token: str) -> None:
        """Accumulate a streaming token into the open response."""
        if self._open_response is None:
            self._open_response = token
        else:
            self._open_response += token
        self._cache = None

    def end_response(self) -> None:
        """Close the open response so the next stream_token starts a fresh one."""
        self._close_open_response()

    # NOTE: begin_response is a no-op here; the waiting indicator is wired in branch 4/5.
    def begin_response(self) -> None:
        return

    # NOTE: turn_finished is a no-op here; post-turn stats live in the status bar (branch 4).
    def turn_finished(self, stats: TurnStats) -> None:
        return

    # NOTE: stream_thinking is a no-op here; the reasoning readout is wired in branch 5.
    def stream_thinking(self, text: str) -> None:
        return

    def show_status(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.dim"))

    def show_error(self, text: str) -> None:
        self._add_renderable(Text(_sanitize_line(text), style="sp.error"))

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        # Redact secrets in the summary line so auto-approved tool calls never
        # expose credentials in the visible pane channel. A `path` argument is
        # shown as its resolved, workspace-relative target (the SAME resolution
        # the tool acts on) so the displayed path cannot be spoofed and matches
        # the file actually touched (design section 14.5).
        redacted = redact_structure(arguments)
        assert isinstance(redacted, dict)
        summary = ", ".join(
            f"{key}={self._tool_call_value(key, value)}" for key, value in redacted.items()
        )
        if len(summary) > 80:
            summary = summary[:79] + self._glyphs.ellipsis
        self._add_renderable(render_tool_call(name, summary, self._glyphs))

    def _tool_call_value(self, key: str, value: object) -> str:
        # A `path` argument is shown as its resolved, workspace-relative target
        # (display-integrity, design section 14.5). Without a workspace the value
        # renders verbatim — matching TerminalUI._tool_call_value exactly.
        if key == "path" and isinstance(value, str) and self._workspace is not None:
            return repr(workspace_display(self._workspace, value))
        return repr(value)

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._add_renderable(render_tool_result(success, summary, self._glyphs))

    def show_command_output(self, line: str) -> None:
        # "    " prefix + control-char sanitization + dim, no markup, no highlight —
        # mirroring TerminalUI (markup=False/highlight=False are implied by Text).
        self._add_renderable(Text("    " + _sanitize_line(line), style="sp.dim"))

    def show_plan_progress(self, plan: TaskPlan) -> None:
        # Uses plan_step_line (not plan_panel) with 2-cell left indent, then a blank
        # line — mirroring TerminalUI.show_plan_progress exactly.
        self._close_open_response()
        for index, step in enumerate(plan.steps, 1):
            self._renderables.append(
                Padding(plan_step_line(index, step, self._glyphs), (0, 0, 0, 2))
            )
        self._renderables.append(Text(""))  # blank line separator
        self._cache = None

    # ------------------------------------------------------------------
    # Approval stubs — raise until branch 7 wires the focus-swap
    # ------------------------------------------------------------------

    def ask_approval(self, request: ApprovalRequest) -> ApprovalReply:
        raise NotImplementedError("approval focus-swap is wired in branch 7")

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        raise NotImplementedError("approval focus-swap is wired in branch 7")
