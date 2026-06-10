"""Planning guidance appended to the system prompt (design sections 11, 19)."""

from __future__ import annotations

PLANNING_GUIDANCE = """\
Planning rules:
- Call propose_plan and wait for approval before: tasks with 3 or more steps, \
file modifications, package installs, or commands that change workspace state.
- Plans are NOT needed for direct answers, single read-only inspections, or \
simple low-risk commands like pwd or running the test suite.
- After approval, execute exactly one step at a time and record progress with \
update_plan(step=N, status="completed").
- When a command fails in a way that invalidates the plan, a file or API does \
not exist, or the same recovery fails twice: stop, record it with \
update_plan(blocker="<evidence>"), and then propose a revised plan or ask the \
user one short, specific question. Never push through a failing path."""
