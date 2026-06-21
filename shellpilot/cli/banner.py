"""Boot-banner renderer for ShellPilot.

Public API:
    render_banner(model: str, *, is_cloud: bool) -> Panel
"""

from __future__ import annotations

from rich.box import ROUNDED
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shellpilot import __version__

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

# Theme color constants (aligned with shellpilot/cli/theme.py).
_COLOR_ACCENT = "#98c379"  # green — local model
_COLOR_WARN = "#e5c07b"  # amber — cloud model
_COLOR_COCKPIT = "#080808"  # near-black for cockpit cells
_GRADIENT_TOP = (0x7C, 0x7C, 0x7C)  # #7c7c7c — nose
_GRADIENT_BOT = (0x3A, 0x3A, 0x3A)  # #3a3a3a — tail


def _lerp_hex(top: tuple[int, int, int], bot: tuple[int, int, int], t: float) -> str:
    r = round(top[0] + (bot[0] - top[0]) * t)
    g = round(top[1] + (bot[1] - top[1]) * t)
    b = round(top[2] + (bot[2] - top[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _build_jet() -> Text:
    """Render the block-art jet as a styled rich Text object."""
    num_rows = len(_JET_BLOCKS)
    t = Text(justify="center")
    for row_idx, (glyph_row, cell_row) in enumerate(zip(_JET_BLOCKS, _JET_CELLS, strict=True)):
        t_frac = row_idx / max(num_rows - 1, 1)
        body_color = _lerp_hex(_GRADIENT_TOP, _GRADIENT_BOT, t_frac)
        row_text = Text()
        for glyph, cell_class in zip(glyph_row, cell_row, strict=True):
            if cell_class == "c":
                row_text.append(glyph, style=f"bold {_COLOR_COCKPIT}")
            elif cell_class == "b":
                row_text.append(glyph, style=body_color)
            else:
                row_text.append(" ")
        # Append this row + newline (except the last row).
        if row_idx < num_rows - 1:
            t.append_text(row_text)
            t.append("\n")
        else:
            t.append_text(row_text)
    return t


def _build_left_col(model: str, *, is_cloud: bool) -> Group:
    model_style = f"bold {_COLOR_WARN if is_cloud else _COLOR_ACCENT}"
    return Group(
        Text("Welcome back, pilot", style="bold", justify="center"),
        Text(""),
        _build_jet(),
        Text(""),
        Text(model, style=model_style, justify="center"),
    )


def _build_right_col() -> Text:
    t = Text()
    t.append("Commands\n", style=f"bold {_COLOR_ACCENT}")
    entries = [
        ("/help", "shortcuts & commands"),
        ("/model", "switch model"),
        ("/plan", "propose a plan"),
        ("/skills", "enable workflow skills"),
        ("/status", "session & locality"),
        ("! <cmd>", "manual shell"),
    ]
    for cmd, desc in entries:
        t.append(f"{cmd:<9}", style="bold")
        t.append(f"{desc}\n", style="dim")
    return t


def render_banner(model: str, *, is_cloud: bool) -> Panel:
    """Return a rich Panel for the boot banner.

    Args:
        model:    The active model name (shown in the left column).
        is_cloud: When True, the model name is styled amber; otherwise green.

    Returns:
        A rich Panel ready to print.
    """
    grid = Table.grid(padding=(0, 3))
    grid.add_column(justify="center")
    grid.add_column(justify="left")
    grid.add_row(_build_left_col(model, is_cloud=is_cloud), _build_right_col())

    tip = Text(
        'Tip: high-risk commands ask you to type "run" before they fire.',
        style="italic dim",
    )
    return Panel(
        Group(grid, Text(""), tip),
        box=ROUNDED,
        title=f"ShellPilot v{__version__}",
        title_align="left",
        border_style="grey50",
        padding=(1, 2),
    )
