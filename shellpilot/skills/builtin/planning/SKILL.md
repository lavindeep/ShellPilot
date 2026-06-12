---
name: planning
description: Execution discipline for an approved multi-step plan.
---
- After the plan is approved the same turn continues. Execute step 1 immediately, record it with update_plan(step=1, status="completed"), and keep going step by step until every step is done or you are blocked. Never stop to announce the next step or to ask for permission you already have.
- When a command fails in a way that invalidates the plan, a file or API does not exist, or the same recovery fails twice: stop, record it with update_plan(blocker="<evidence>"), then propose a revised plan or ask the user one short, specific question. Never push through a failing path.
