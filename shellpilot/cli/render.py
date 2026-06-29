"""Pure renderable builders for the v2 visual design (design section 31).

Every function returns a rich renderable and performs no I/O. All borders come
from rich primitives (Panel) — never hand-assembled strings — so alignment is
guaranteed at any terminal width. Styles are theme names from cli/theme.py.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from rich import box
from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from shellpilot.cli.theme import Glyphs
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.planner import PlanStep, TaskPlan

WORD_HIGHLIGHT_RATIO = 0.5

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_BADGE_STYLES = {
    "low": "sp.badge.medium",
    "medium": "sp.badge.medium",
    "high": "sp.badge.high",
    "blocked": "sp.badge.blocked",
}


def _abbreviate_home(path: Path, home: Path) -> str:
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    rel_str = str(rel)
    return "~" if rel_str == "." else f"~/{rel_str}"


def context_line(
    workspace: Path,
    model: str,
    profile: str,
    *,
    home: Path | None = None,
    max_width: int = 80,
) -> Text:
    suffix = f" · {model} · {profile}"
    path_str = _abbreviate_home(workspace, home or Path.home())
    budget = max(8, max_width - len(suffix))
    if len(path_str) > budget:
        keep = budget - 1
        head = keep // 2
        path_str = path_str[:head] + "…" + path_str[-(keep - head) :]
    return Text(path_str + suffix, style="sp.dim")


def tool_call(name: str, args_summary: str, glyphs: Glyphs) -> Text:
    # Generic fallback line for tools with no clean primary subject (§31.3): name
    # bright, the key=value summary dim so routine plumbing doesn't shout. Tools
    # with a subject route through tool_call_block (framed or inline) instead.
    return Text.assemble(
        (f"{glyphs.bullet} ", ""),
        (_sanitize_line(name), "sp.emph"),
        (f"({_sanitize_line(args_summary)})", "sp.dim"),
    )


# Built-in tools whose primary action deserves a framed, high-contrast subject
# (§31.3): the command that runs, the URL that egresses. Their tool-call line is
# `⏺ <label>` followed by a bordered box holding the actual subject.
_FRAMED_LABELS = {"run_command": "run command", "web_fetch": "web_fetch"}

# The single argument that IS the subject for each built-in tool that has one
# (`⏺ name  subject`, readable, not the name(key=repr) form). run_command is the
# exception — its subject is the joined argv, handled directly below. A `path`
# subject is resolved (display-integrity, §14.5) by the caller.
_SUBJECT_KEY = {
    "read_file": "path",
    "list_dir": "path",
    "view_image": "path",
    "write_file": "path",
    "patch_file": "path",
    "web_search": "query",
    "search_text": "pattern",
    "skill_read": "resource",
    "web_fetch": "url",
}


def _tool_subject(
    name: str, redacted: dict[str, object], path_display: Callable[[str], str]
) -> str | None:
    """The clean primary subject for a built-in tool, or None to fall back.

    `redacted` is the secret-redacted argument dict; a `path` subject is run
    through `path_display` so the shown target is the resolved, workspace-relative
    one the handler acts on (never the raw, possibly spoofing arg, §14.5).
    """
    if name == "run_command":
        argv = redacted.get("argv")
        if isinstance(argv, list) and argv:
            return " ".join(str(token) for token in argv)
        return None
    key = _SUBJECT_KEY.get(name)
    if key is None:
        return None
    value = redacted.get(key)
    if not isinstance(value, str) or not value:
        return None
    return path_display(value) if key == "path" else value


def _arg_summary(
    redacted: dict[str, object], glyphs: Glyphs, path_display: Callable[[str], str]
) -> str:
    # The generic key=value summary. A `path` value is resolved (§14.5); the line
    # is capped so a chatty argument set can't run off the side.
    parts = []
    for key, value in redacted.items():
        if key == "path" and isinstance(value, str):
            parts.append(f"{key}={path_display(value)!r}")
        else:
            parts.append(f"{key}={value!r}")
    summary = ", ".join(parts)
    if len(summary) > 80:
        summary = summary[:79] + glyphs.ellipsis
    return summary


def tool_call_block(
    name: str,
    redacted: dict[str, object],
    glyphs: Glyphs,
    *,
    path_display: Callable[[str], str],
) -> list[RenderableType]:
    """Renderable(s) for one tool-call line (§31.3).

    Pure: `redacted` is the already secret-redacted argument dict and `path_display`
    resolves a path arg to its workspace-relative display — no I/O here. Returns a
    framed [header, box] for the consequential tools (run_command, web_fetch), a
    single inline `⏺ name  subject` line for tools with a clean primary subject,
    or the generic `⏺ name(args)` line for everything else.
    """
    subject = _tool_subject(name, redacted, path_display)
    label = _FRAMED_LABELS.get(name)
    if subject and label is not None:
        header = Text.assemble((f"{glyphs.bullet} ", ""), (_sanitize_line(label), "sp.emph"))
        frame = Panel(
            Text(_sanitize_line(subject), style="sp.cmd"),
            box=box.ROUNDED,
            border_style="sp.faint",
            expand=False,
            padding=(0, 1),
        )
        return [header, frame]
    if subject:
        return [
            Text.assemble(
                (f"{glyphs.bullet} ", ""),
                (_sanitize_line(name), "sp.emph"),
                (f"  {_sanitize_line(subject)}", "sp.value"),
            )
        ]
    return [tool_call(name, _arg_summary(redacted, glyphs, path_display), glyphs)]


def tool_result(success: bool, summary: str, glyphs: Glyphs) -> Text:
    mark, style = (glyphs.check, "sp.success") if success else (glyphs.cross, "sp.error")
    return Text.assemble(
        (f"  {glyphs.elbow} ", "sp.dim"),
        (mark, style),
        (f" {_sanitize_line(summary)}", "sp.dim"),
    )


_TOKEN = re.compile(r"\w+|\W")


def _token_offsets(line: str) -> tuple[list[str], list[int]]:
    tokens = _TOKEN.findall(line)
    offsets = [0]
    for token in tokens:
        offsets.append(offsets[-1] + len(token))
    return tokens, offsets


def word_highlight_ranges(
    old: str, new: str, threshold: float = WORD_HIGHLIGHT_RATIO
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """Char ranges to highlight in a removed/added pair, or None if dissimilar.

    Diffs whole word tokens (not characters) so changed words highlight as
    units instead of speckling on shared letters.
    """
    if SequenceMatcher(None, old, new, autojunk=False).ratio() < threshold:
        return None
    old_tokens, old_offsets = _token_offsets(old)
    new_tokens, new_offsets = _token_offsets(new)
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            old_ranges.append((old_offsets[i1], old_offsets[i2]))
        if tag in ("replace", "insert") and j2 > j1:
            new_ranges.append((new_offsets[j1], new_offsets[j2]))
    return old_ranges, new_ranges


def _sanitize_line(line: str) -> str:
    return _CONTROL_CHARS.sub("", line.rstrip("\r").expandtabs(4))


def _gutter_width(lines: list[str]) -> int:
    max_no = 1
    for line in lines:
        match = _HUNK_HEADER.match(line)
        if match:
            old_end = int(match.group(1)) + int(match.group(2) or 1)
            new_end = int(match.group(3)) + int(match.group(4) or 1)
            max_no = max(max_no, old_end, new_end)
    return len(str(max_no))


def _diff_row(
    number: int,
    width: int,
    marker: str,
    content: str,
    base_style: str | None,
    word_style: str | None,
    ranges: list[tuple[int, int]] | None,
    *,
    pad_width: int = 0,
) -> Text:
    row = Text()
    row.append(f"{number:>{width}} ", style="sp.diff.gutter")
    prefix = len(row) + 2  # gutter + marker + space
    # Pad the content under the colored background so a changed line renders as a
    # full-width red/green bar (DESIGN section 31.4 full-line backgrounds): the
    # removal and its addition then read as two distinct rows, never one. The
    # padding rides inside base_style so the fill is colored; word ranges sit at
    # the content start and are unaffected by trailing spaces.
    if base_style is not None and pad_width:
        content = content + " " * max(0, pad_width - cell_len(content))
    row.append(f"{marker} {content}", style=base_style)
    if ranges and word_style:
        for start, end in ranges:
            row.stylize(word_style, prefix + start, prefix + end)
    return row


def _diff_rows(
    diff_text: str, glyphs: Glyphs, *, width: int | None = None
) -> tuple[list[Text], str]:
    """Parse a unified diff into rendered rows plus the panel title name.

    Shared by :func:`render_diff` and the streaming ``DiffReveal`` so the reveal
    animation and the settled panel never drift in how a diff is rendered.

    *width* is the target panel width (§31.16). When given, changed-line bars
    fill to the panel's INNER width so every bar is full at the standardized
    size; a line longer than that target keeps its length and folds at render
    time (the pad clamp leaves it untouched). ``None`` (the default for
    ``DiffReveal`` and unsized callers) pads to the widest changed line as before.
    """
    lines = [_sanitize_line(line) for line in diff_text.splitlines()]
    gutter = _gutter_width(lines)
    if width is not None:
        # Inner content region = width - 2 (borders) - 2 (padding); a row is the
        # gutter ("{n} ", gutter+1 chars) + "{marker} {content}" (2 + content), so
        # content fills the region when its length is width - gutter - 7.
        pad_width = max(0, width - gutter - 7)
    else:
        # Widest changed-line content, so every removal/addition fills its colored
        # background to a uniform width (full-line red/green bars, DESIGN 31.4).
        pad_width = max(
            (
                cell_len(line[1:])
                for line in lines
                if (line.startswith("-") and not line.startswith("---"))
                or (line.startswith("+") and not line.startswith("+++"))
            ),
            default=0,
        )
    title_name = "diff"
    rows: list[Text] = []
    old_no = new_no = 0
    first_hunk = True
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            title_name = line[4:].strip().removeprefix("b/")
            i += 1
        elif line.startswith("--- ") or not line:
            i += 1
        elif line.startswith("@@"):
            match = _HUNK_HEADER.match(line)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(3))
            if not first_hunk:
                rows.append(Text(glyphs.ellipsis, style="sp.faint"))
            first_hunk = False
            i += 1
        elif line.startswith("-"):
            removed: list[str] = []
            while i < len(lines) and lines[i].startswith("-") and not lines[i].startswith("---"):
                removed.append(lines[i][1:])
                i += 1
            added: list[str] = []
            while i < len(lines) and lines[i].startswith("+") and not lines[i].startswith("+++"):
                added.append(lines[i][1:])
                i += 1
            old_words: dict[int, list[tuple[int, int]]] = {}
            new_words: dict[int, list[tuple[int, int]]] = {}
            for idx in range(min(len(removed), len(added))):
                result = word_highlight_ranges(removed[idx], added[idx])
                if result is not None:
                    old_words[idx], new_words[idx] = result
            for idx, content in enumerate(removed):
                rows.append(
                    _diff_row(
                        old_no,
                        gutter,
                        "-",
                        content,
                        "sp.diff.remove",
                        "sp.diff.remove_word",
                        old_words.get(idx),
                        pad_width=pad_width,
                    )
                )
                old_no += 1
            for idx, content in enumerate(added):
                rows.append(
                    _diff_row(
                        new_no,
                        gutter,
                        "+",
                        content,
                        "sp.diff.add",
                        "sp.diff.add_word",
                        new_words.get(idx),
                        pad_width=pad_width,
                    )
                )
                new_no += 1
        elif line.startswith("+"):
            rows.append(
                _diff_row(
                    new_no, gutter, "+", line[1:], "sp.diff.add", None, None, pad_width=pad_width
                )
            )
            new_no += 1
            i += 1
        elif line.startswith("\\") or line.startswith("... ("):
            rows.append(Text(line, style="sp.faint"))
            i += 1
        else:
            content = line[1:] if line.startswith(" ") else line
            rows.append(_diff_row(new_no, gutter, " ", content, None, None, None))
            old_no += 1
            new_no += 1
            i += 1
    return rows, title_name


def render_diff(
    diff_text: str, glyphs: Glyphs, *, max_rows: int | None = None, width: int | None = None
) -> Panel:
    """An editor-style diff panel: gutter, line backgrounds, word highlights.

    When *max_rows* is set and the rendered diff exceeds it, the panel keeps the
    first ``max_rows`` rows and appends one ``… (+N more)`` footer (``sp.faint``,
    mirroring :func:`output_truncation`). ``max_rows=None`` (the default at every
    existing call site) renders the full diff unchanged.

    *width* standardizes the panel to that fixed width (§31.16): bars fill to the
    inner width and over-long lines fold onto continuation rows instead of running
    off the side. ``None`` keeps the legacy hug-the-widest-line sizing.
    """
    rows, title_name = _diff_rows(diff_text, glyphs, width=width)
    if max_rows is not None and len(rows) > max_rows:
        hidden = len(rows) - max_rows
        rows = rows[:max_rows]
        rows.append(Text(f"{glyphs.ellipsis} (+{hidden} more)", style="sp.faint"))
    body: Group | Text = Group(*rows) if rows else Text("(no changes)", style="sp.dim")
    return Panel(
        body,
        title=Text(title_name, style="sp.emph"),
        title_align="left",
        box=box.ROUNDED,
        border_style="sp.faint",
        expand=False,
        width=width,
        padding=(0, 1),
    )


def diff_row_count(diff_text: str, glyphs: Glyphs) -> int:
    """Rendered row count of a diff — what a collapse cap is compared against.

    Lets a caller decide whether a diff overflows a row cap (and thus needs a
    collapse/expand affordance) without rendering the panel.
    """
    return len(_diff_rows(diff_text, glyphs)[0])


def badge(level: str, *, plain: bool = False) -> Text:
    label = level.upper()
    if plain:
        return Text(f"[{label}]")
    return Text(f" {label} ", style=_BADGE_STYLES.get(level.lower(), "sp.badge.medium"))


def _stat_row(info: Text, label: str, value: str) -> None:
    # One stat-block row (§31.5): a muted fixed-width label and a readable value.
    # The value is sanitized — defense in depth even though reasons/purpose are
    # classifier/template strings, not model output. Callers own the line break
    # between rows so a standalone row (approval_cwd) carries no leading blank.
    info.append(f"  {label:<6} ", style="sp.label")
    info.append(_sanitize_line(value), style="sp.value")


def approval_info(request: ApprovalRequest, *, plain_badge: bool = False) -> Text:
    # Stat block (§31.5): a colored risk badge, then muted labels with readable
    # values, so WHY the action is gated and its EFFECT can't be missed — the old
    # single dim "kind · reasons · purpose" line buried all of it in gray.
    info = Text("  ")
    info.append_text(badge(request.risk.value, plain=plain_badge))
    if request.reasons:
        info.append("\n")
        _stat_row(info, "WHY", " · ".join(request.reasons))
    if request.purpose:
        info.append("\n")
        _stat_row(info, "EFFECT", request.purpose)
    return info


def approval_cwd(request: ApprovalRequest) -> Text:
    info = Text()
    _stat_row(info, "CWD", str(request.cwd))
    return info


def approval_choices(request: ApprovalRequest) -> Text:
    """The actionable choice line (§31.5): yes=green, edit=amber, no=red.

    A HIGH-risk *command* requires typing "run" (red); a HIGH-risk *tool* (a
    sensitive read) keeps the standard y/e/n prompt. Display only — the input
    parsing in the approval gates is unchanged, so the literal tokens stay put.
    """
    if request.risk is RiskLevel.HIGH and request.kind == "command":
        run = Text("  Type ")
        run.append('"run"', style="sp.risk.high")
        run.append(" to execute, ")
        run.append("[e]dit", style="sp.choice.edit")
        run.append(" to steer, or press Enter to cancel: ")
        return run
    return _yes_edit_no("  Approve? ")


def plan_choices() -> Text:
    """The plan-approval choice line (§31.5): same colored y/e/n as commands."""
    return _yes_edit_no("  Approve plan? ")


def _yes_edit_no(lead: str) -> Text:
    line = Text(lead)
    line.append("[y]es", style="sp.choice.yes")
    line.append(" / ")
    line.append("[e]dit", style="sp.choice.edit")
    line.append(" / ")
    line.append("[n]o", style="sp.choice.no")
    line.append(" ")
    return line


def plan_step_line(index: int, step: PlanStep, glyphs: Glyphs) -> Text:
    title = _sanitize_line(step.title)
    if step.status == "completed":
        return Text.assemble(
            (f"{glyphs.check} {index}", "sp.success"),
            (f"  {title}", "sp.dim"),
        )
    if step.status == "active":
        return Text(f"{glyphs.current} {index}  {title}", style="sp.emph")
    if step.status == "skipped":
        return Text(f"{glyphs.skip} {index}  {title}", style="sp.dim")
    return Text(f"{glyphs.todo} {index}  {title}", style="sp.dim")


def plan_panel(plan: TaskPlan, glyphs: Glyphs) -> Panel:
    rows: list[Text] = [
        Text.assemble(("Goal: ", "sp.dim"), (_sanitize_line(plan.goal), "")),
        Text(""),
    ]
    rows.extend(plan_step_line(i, step, glyphs) for i, step in enumerate(plan.steps, 1))
    return Panel(
        Group(*rows),
        title=Text.assemble(("Plan", "sp.emph"), (f" · {plan.task_id}", "sp.dim")),
        title_align="left",
        box=box.ROUNDED,
        border_style="sp.faint",
        expand=False,
        padding=(0, 1),
    )


def output_truncation(hidden_lines: int, glyphs: Glyphs) -> Text:
    return Text(f"    {glyphs.ellipsis} +{hidden_lines} lines", style="sp.faint")
