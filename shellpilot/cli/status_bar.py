"""Persistent bottom status bar — the at-a-glance dock above the input.

A Claude-Code-style one-line bar pinned just above the prompt (rendered as the
input's ``bottom_toolbar``, so it stays static and refreshes each render). It
folds two previously-separate readouts into one always-on place:

  - the unspoofable active-cloud indicator (design section 15.2), and
  - context utilization, formerly printed after every reply.

Layout (prototype "V1", design section 32):
    LEFT   <dir> · <model> · <profile> · <locality>
    RIGHT  <pct>% ctx

``status_bar`` is a PURE builder mirroring ``cli/banner.py``: it returns a
prompt_toolkit ``FormattedText`` list and performs no I/O. dir/model/profile/
ctx come from runtime/session state — never from the model — and the locality
segment is gated on the caller's real ``is_egressing`` signal, keeping the
cloud indicator harness-rendered and unspoofable. The workspace path is
user-controlled, so it is stripped of control/ANSI bytes before render.
"""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText

from shellpilot.cli.render import _abbreviate_home, _sanitize_line

# Theme color constants (hex strings; aligned with shellpilot/cli/theme.py).
# prompt_toolkit cannot read the rich Theme, so the bar mirrors the same hexes,
# the same pattern cli/banner.py uses for its rich-independent jet art.
COLOR_ACCENT = "#98c379"  # green — local model / low ctx
COLOR_WARN = "#e5c07b"  # amber — cloud model / mid ctx / egress emphasis
COLOR_ERROR = "#e06c75"  # red — high ctx
COLOR_DIM = "#6b6b6b"  # sp.dim — ordinary segment text
COLOR_FAINT = "#444444"  # sp.faint — separators

# Context-utilization color thresholds (percent full): green below MID, amber
# from MID up to HI, red at/above HI.
CTX_MID = 50
CTX_HI = 80


def ctx_percent(used_tokens: int, total_tokens: int) -> int:
    """Context utilization as an integer percent, rounded and clamped to 0–100.

    Mirrors the runtime's own post-turn formula so the always-on bar and the
    (now removed) per-turn line never disagree; a zero budget never divides.
    """
    if total_tokens <= 0:
        return 0
    return min(100, round(100 * used_tokens / total_tokens))


def _ctx_color(pct: int) -> str:
    if pct >= CTX_HI:
        return COLOR_ERROR
    if pct >= CTX_MID:
        return COLOR_WARN
    return COLOR_ACCENT


def status_bar(
    *,
    workspace: Path,
    model: str,
    profile: str,
    is_cloud: bool,
    ctx_pct: int,
    home: Path | None = None,
) -> FormattedText:
    """Build the persistent status-bar fragments for the input bottom toolbar.

    Args:
        workspace: Active workspace path (shown home-abbreviated, sanitized).
        model:     Active model name — GREEN when local, AMBER when egressing.
        profile:   Security profile name (e.g. ``balanced``).
        is_cloud:  The caller's REAL ``is_egressing`` signal. When True the bar
                   carries an unmistakable amber emphasis and a ``☁ CLOUD``
                   locality; this is the harness-rendered active-cloud indicator
                   (design section 15.2) — never derived from model output.
        ctx_pct:   Context utilization percent (see ``ctx_percent``); color-
                   coded green → amber → red as it fills.
        home:      Home directory for abbreviation (defaults to ``Path.home()``).

    Returns:
        A prompt_toolkit ``FormattedText`` (list of ``(style, text)`` fragments).
    """
    # Sanitize the user-controlled path before it reaches the terminal (Group B):
    # strip control/ANSI bytes so a crafted directory name cannot repaint the UI.
    dir_text = _sanitize_line(_abbreviate_home(workspace, home or Path.home()))
    model_text = _sanitize_line(model)
    profile_text = _sanitize_line(profile)

    # When egressing, separators carry a faint amber tint — the "wash" adapted to
    # a terminal — so the whole bar reads as cloud at a glance, distinct from a
    # local (faint-grey) bar. Locality and model also go amber below.
    sep_color = COLOR_WARN if is_cloud else COLOR_FAINT
    model_color = COLOR_WARN if is_cloud else COLOR_ACCENT
    sep = (f"fg:{sep_color}", " · ")

    if is_cloud:
        locality = (f"fg:{COLOR_WARN} bold", "☁ CLOUD")
    else:
        locality = (f"fg:{COLOR_ACCENT}", "● local")

    left: list[tuple[str, str]] = [
        (f"fg:{COLOR_DIM}", dir_text),
        sep,
        (f"fg:{model_color}", model_text),
        sep,
        (f"fg:{COLOR_DIM}", profile_text),
        sep,
        locality,
    ]

    right: list[tuple[str, str]] = [
        (f"fg:{_ctx_color(ctx_pct)}", f"{ctx_pct}%"),
        (f"fg:{COLOR_DIM}", " ctx"),
    ]

    gap = (f"fg:{sep_color}", "   ")
    return FormattedText([*left, gap, *right])
