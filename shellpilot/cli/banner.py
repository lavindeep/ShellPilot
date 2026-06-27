"""Boot-banner renderer for ShellPilot.

Public API:
    render_banner(model, *, is_cloud, profile, skills=(), recent_sessions=()) -> Panel
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.align import Align
from rich.box import ROUNDED, Box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shellpilot import __version__
from shellpilot.cli.render import _sanitize_line
from shellpilot.cli.theme import COLOR_ACCENT, COLOR_DIM, COLOR_FAINT, COLOR_WARN

# ---------------------------------------------------------------------------
# Jet art — locked v2 block art, embedded verbatim (do NOT read from disk).
# Each row is 28 chars wide; trailing spaces are significant.
# ---------------------------------------------------------------------------

_JET_BLOCKS = (
    "             ██             ",
    "            ▄██▄            ",
    "            ████            ",
    "            ████            ",
    "           ▄████▄           ",
    "          ████████          ",
    "          ████████          ",
    "         ██████████         ",
    "        ▄██████████▄        ",
    "     ▄████████████████▄     ",
    "   ▄████████████████████▄   ",
    " ▄████████████████████████▄ ",
    "████████████████████████████",
    " ▀▀██████████████████████▀▀ ",
    "     ▀▀██████████████▀▀     ",
    "      ▄██████████████▄      ",
    "     ██████▀ ▀▀ ▀██████     ",
    "     ▀▀██▀▀      ▀▀██▀▀     ",
)

# Parallel class grid: '.' = empty, 'b' = body, 'c' = cockpit.
_JET_CELLS = (
    ".............bb.............",
    "............bbbb............",
    "............bccb............",
    "............bccb............",
    "...........bbccbb...........",
    "..........bbbbbbbb..........",
    "..........bbbbbbbb..........",
    ".........bbbbbbbbbb.........",
    "........bbbbbbbbbbbb........",
    ".....bbbbbbbbbbbbbbbbbb.....",
    "...bbbbbbbbbbbbbbbbbbbbbb...",
    ".bbbbbbbbbbbbbbbbbbbbbbbbbb.",
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ".bbbbbbbbbbbbbbbbbbbbbbbbbb.",
    ".....bbbbbbbbbbbbbbbbbb.....",
    "......bbbbbbbbbbbbbbbb......",
    ".....bbbbbbb.bb.bbbbbbb.....",
    ".....bbbbbb......bbbbbb.....",
)

_JET_WIDTH = len(_JET_BLOCKS[0])  # 28 — every row is this wide.

# Banner-only color constants (shared theme colors imported from theme.py above).
_COLOR_CYAN = "#7fb3c8"  # section headers
_COLOR_COCKPIT = "#080808"  # near-black for cockpit cells
_GRADIENT_TOP = (0x7C, 0x7C, 0x7C)  # #7c7c7c — nose
_GRADIENT_BOT = (0x3A, 0x3A, 0x3A)  # #3a3a3a — tail

# Available built-in workflow skills shown (dim) as the enable hint when none
# are enabled. These are real builtins under skills/builtin/.
_AVAILABLE_WORKFLOW_SKILLS = ("debugging", "verification", "code-review", "git-workflow")

# Box drawing ONLY the inner vertical divider between the two columns — no
# edge, no top/bottom caps, no horizontal section rules. The third char of each
# line is the column divider (see rich.box format); everything else is blank so
# the divider spans the full content height as a clean `│`.
_DIVIDER_BOX = Box(
    "    \n"  # top
    "  │ \n"  # head
    "    \n"  # head_row
    "  │ \n"  # mid
    "    \n"  # row
    "    \n"  # foot_row
    "  │ \n"  # foot
    "    \n"  # bottom
)


def _lerp_hex(top: tuple[int, int, int], bot: tuple[int, int, int], t: float) -> str:
    r = round(top[0] + (bot[0] - top[0]) * t)
    g = round(top[1] + (bot[1] - top[1]) * t)
    b = round(top[2] + (bot[2] - top[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _build_jet() -> Text:
    """The block-art jet as one fixed-width, left-aligned Text block.

    Every row is exactly ``_JET_WIDTH`` cells of real characters (empty cells
    are literal spaces, never trimmed). Rendered as a left-aligned block so the
    containing grid column centers the whole jet as a unit — NOT per line via
    ``justify="center"``, which Rich would skew by stripping trailing spaces.
    """
    num_rows = len(_JET_BLOCKS)
    jet = Text(no_wrap=True, justify="left")
    for row_idx, (glyph_row, cell_row) in enumerate(zip(_JET_BLOCKS, _JET_CELLS, strict=True)):
        t_frac = row_idx / max(num_rows - 1, 1)
        body_color = _lerp_hex(_GRADIENT_TOP, _GRADIENT_BOT, t_frac)
        for glyph, cell_class in zip(glyph_row, cell_row, strict=True):
            if cell_class == "c":
                jet.append(glyph, style=f"bold {_COLOR_COCKPIT}")
            elif cell_class == "b":
                jet.append(glyph, style=body_color)
            else:
                jet.append(" ")
        if row_idx < num_rows - 1:
            jet.append("\n")
    return jet


def _build_left_col(model: str, *, is_cloud: bool, profile: str) -> Group:
    model_style = f"bold {COLOR_WARN if is_cloud else COLOR_ACCENT}"
    locality = "cloud" if is_cloud else "local"
    # The jet is centered as a fixed-width BLOCK via Align (width=_JET_WIDTH),
    # not per line — per-line centering skews because Rich strips trailing
    # spaces, leaving narrow rows lopsided in a live terminal.
    return Group(
        Text("Welcome back, pilot", style="bold", justify="center"),
        Text(""),
        Align.center(_build_jet(), width=_JET_WIDTH),
        Text(""),
        Text(model, style=model_style, justify="center"),
        Text(f"{profile} · {locality}", style=COLOR_DIM, justify="center"),
    )


# Right-column rule width: matches the widest section item so the horizontal
# rules stay bounded to the column rather than spanning the full panel.
_RULE_WIDTH = 40
_ITEM_PAD = 9  # left field for the bold lead token, e.g. "/help    "


def _section(header: str, items: Sequence[tuple[str, str]]) -> Text:
    t = Text()
    t.append(header, style=f"bold {_COLOR_CYAN}")
    for lead, desc in items:
        t.append("\n")
        t.append(f"{lead:<{_ITEM_PAD}}", style="bold")
        t.append(desc, style=COLOR_DIM)
    return t


def _rule() -> Text:
    return Text("─" * _RULE_WIDTH, style=COLOR_FAINT)


def _workflow_section(skills: Sequence[str]) -> Text:
    t = Text()
    t.append("Workflow skills", style=f"bold {_COLOR_CYAN}")
    t.append("\n")
    if skills:
        t.append(" · ".join(skills), style=COLOR_DIM)
    else:
        t.append(" · ".join(_AVAILABLE_WORKFLOW_SKILLS), style=COLOR_DIM)
        t.append("\n")
        t.append("/skills to enable", style=COLOR_FAINT)
    return t


def _recent_section(recent_sessions: Sequence[tuple[str, str]]) -> Text:
    t = Text()
    t.append("Recent sessions", style=f"bold {_COLOR_CYAN}")
    for label, age in recent_sessions:
        t.append("\n")
        t.append("● ", style=COLOR_DIM)
        # The label is a snippet of a past session's first USER message — untrusted,
        # possibly-pasted input. Strip control/ANSI bytes at this render sink so a
        # stored escape sequence cannot repaint the terminal on boot (Group B).
        t.append(_sanitize_line(label))
        t.append(f" ({age})", style=COLOR_DIM)
    return t


def _build_right_col(skills: Sequence[str], recent_sessions: Sequence[tuple[str, str]]) -> Group:
    blocks: list[RenderableType] = [
        _section(
            "Commands",
            (
                ("/help", "shortcuts & commands"),
                ("/plan", "propose a plan"),
                ("/skills", "enable workflow skills"),
                ("/status", "session & locality"),
            ),
        ),
        _rule(),
        _section(
            "Tips",
            (
                ("/", "for slash commands"),
                ("!", "to run a shell command"),
                ('"run"', "to confirm a high-risk command"),
            ),
        ),
        _rule(),
        _workflow_section(skills),
    ]
    if recent_sessions:
        blocks.append(_rule())
        blocks.append(_recent_section(recent_sessions))
    return Group(*blocks)


def render_banner(
    model: str,
    *,
    is_cloud: bool,
    profile: str,
    skills: Sequence[str] = (),
    recent_sessions: Sequence[tuple[str, str]] = (),
) -> Panel:
    """Return a rich Panel for the boot banner.

    Args:
        model:    The active model name (left column; green local / amber cloud).
        is_cloud: When True, the model name is styled amber and the sub-line
                  reads "cloud"; otherwise green and "local".
        profile:  Security profile name, shown in the dim sub-line.
        skills:   Enabled workflow-skill names. When empty, the Workflow skills
                  section shows the available builtins plus an enable hint.
        recent_sessions: (label, age) pairs, newest first. The whole Recent
                  sessions section is omitted when this is empty.

    Returns:
        A bounded-width (expand=False) rich Panel ready to print.
    """
    grid = Table(box=_DIVIDER_BOX, show_header=False, pad_edge=False, padding=(0, 3))
    grid.add_column(justify="center", vertical="middle")
    grid.add_column(justify="left", vertical="top")
    grid.add_row(
        _build_left_col(model, is_cloud=is_cloud, profile=profile),
        _build_right_col(skills, recent_sessions),
    )
    return Panel(
        grid,
        box=ROUNDED,
        title=f"ShellPilot v{__version__}",
        title_align="left",
        border_style="grey50",
        padding=(1, 2),
        expand=False,
    )
