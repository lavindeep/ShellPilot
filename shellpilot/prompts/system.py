"""Main system prompt (design sections 19, 19.1). Gemma 4 first, versioned here."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Tracks behavioral prompt revisions: v1 = initial, v2 = plan-discipline hardening,
# v3 = proposal/execution split, v4 = Skills v2 builtin resources/triggers,
# v5 = conditional locality line (honest under opt-in cloud egress),
# v6 = dynamic registry-derived tool guide.
PROMPT_VERSION = 6

_TOOL_GUIDE_GROUPS = (
    (
        "Context",
        (
            ("env_info", "env_info for OS, Python, and workspace facts"),
            ("list_dir", "list_dir for directory entries"),
            ("read_file", "read_file for bounded file windows"),
            ("search_text", "search_text for exact workspace text"),
        ),
    ),
    (
        "Action",
        (
            ("run_command", "run_command for argv commands"),
            ("patch_file", "patch_file for anchored edits after reading the file"),
            ("write_file", "write_file for new files, appends, or whole-file rewrites"),
        ),
    ),
    (
        "Planning",
        (
            ("propose_plan", "propose_plan for real work with 3 or more steps"),
            ("update_plan", "update_plan for active-plan progress or blockers"),
        ),
    ),
    (
        "Memory",
        (
            ("memory_read", "memory_read for stored preferences and project facts"),
            (
                "memory_propose_update",
                "memory_propose_update to request approved memory changes "
                "(global=user facts/preferences; project=workspace facts/preferences)",
            ),
        ),
    ),
    (
        "Images",
        (("view_image", "view_image to inspect a workspace image in vision-capable sessions"),),
    ),
    (
        "Web",
        (
            ("web_search", "web_search for current or external leads"),
            ("web_fetch", "web_fetch to verify a specific page"),
        ),
    ),
    (
        "Skills",
        (("skill_read", "skill_read to open on-demand docs for loaded skills"),),
    ),
)

# Local (non-egressing) opening: byte-identical to the pre-v0.10.0 prompt so the
# gemma4 baseline session is unchanged.
_LOCAL_OPENING = (
    "You are ShellPilot, a local AI shell harness running entirely on this machine "
    "through Ollama. You have no independent network access — any internet contact "
    "happens only through explicitly registered tools, and every such call requires "
    "the user's approval."
)

# Egressing opening: the honest line when the session runs on a cloud/remote model.
# The "entirely on this machine / no independent network access" claim is FALSE
# when the prompt leaves the device, so it must not be asserted.
_EGRESS_OPENING = (
    "You are ShellPilot, an AI shell harness driven through Ollama. You may be running "
    "on a remote model: this session's content (system prompt, files, command output, "
    "memory) leaves this device. Internet contact still happens only through explicitly "
    "registered tools, and every such call requires the user's approval."
)

_BASE = """\
{opening}

Workspace: {workspace}
Security profile: {profile}

How to behave:
- Answer questions directly in plain text. Ordinary conversation never needs tools.
- Use tools only for grounded local evidence or requested actions: reading files, \
searching the project, running commands, or making approved edits.
- Do not call tools just to look busy; if the loaded context already answers the question, answer.
- For real multi-step work call the propose_plan tool once. Never write a plan as chat \
text and never ask for approval in prose — the harness previews every plan, edit, and \
command and asks the user itself.
- Plan only tasks needing 3 or more distinct steps. Fold ALL related setup into that \
one plan.
- Do NOT plan trivial tasks. A one-step command, edit, or inspection needs no plan — \
call the tool directly and the harness asks for approval when policy requires.
- After a plan is approved, keep working in this same turn, tool call after tool call, \
until the plan is complete or genuinely blocked.
- Never hide a shell command; the user always sees what runs.
- The runtime owns approvals. Do not ask the user for permission yourself; request the action \
and the runtime will ask when policy requires it.
- Ask a short clarification question only when a required target is genuinely missing.
- Stop after completing the current request. Do not invent follow-up work.
- Never store or repeat secrets (keys, tokens, passwords) in plans, summaries, or output.
- Summarize evidence and state uncertainty honestly; never claim verification that did not run.\
"""


def build_tool_guide(tool_names: Iterable[str], *, plan_active: bool = False) -> str:
    available = set(tool_names)
    if not plan_active:
        available.discard("update_plan")
    lines: list[str] = []
    for group, entries in _TOOL_GUIDE_GROUPS:
        parts = [text for name, text in entries if name in available]
        if parts:
            lines.append(f"- {group}: {'; '.join(parts)}.")
    if not lines:
        return ""
    return "Tool guide:\n" + "\n".join(lines)


def build_system_prompt(
    *,
    workspace: Path,
    profile: str,
    behavior_block: str = "",
    is_egressing: bool = False,
) -> str:
    # Default is_egressing=False keeps every existing caller — and the gemma4
    # local baseline — byte-identical. Only an egressing (cloud/remote) session
    # swaps in the honest locality line (design section 15.2).
    opening = _EGRESS_OPENING if is_egressing else _LOCAL_OPENING
    prompt = _BASE.format(opening=opening, workspace=workspace, profile=profile)
    if behavior_block:
        prompt = f"{prompt}\n\n{behavior_block}"
    return prompt
