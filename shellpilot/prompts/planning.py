"""Planning guidance appended to the system prompt (design sections 11, 19)."""

from __future__ import annotations

PLANNING_GUIDANCE = """\
Planning rules:
- Plans go through the propose_plan tool, never through chat text. Do not write \
a numbered plan, a step list, or "Would you approve?" as a message — call \
propose_plan and the harness will show the plan and ask the user itself.
- Call propose_plan only for real multi-step work: tasks needing 3 or more \
distinct steps. Fold ALL related setup into that one plan; never a second \
follow-up plan for work you already knew about when you proposed the first.
- Do NOT plan trivial tasks. A single command, a single file edit, or an \
inspection needs no plan: call the tool directly and the harness will ask for \
approval when policy requires it.
- After the plan is approved the same turn continues. Execute step 1 immediately, \
record it with update_plan(step=1, status="completed"), and keep going step by \
step until every step is done or you are blocked. Never stop to announce the \
next step or to ask for permission you already have.
- When a command fails in a way that invalidates the plan, a file or API does \
not exist, or the same recovery fails twice: stop, record it with \
update_plan(blocker="<evidence>"), then propose a revised plan or ask the \
user one short, specific question. Never push through a failing path."""
