Call propose_plan once for real multi-step work — include every step you can foresee: investigation, each change, and how you'll verify it.

Write each step as one concrete action you can carry out and check — name the command, file, or check (run `pytest -q`; edit config.toml to set X; grep for callers of Y). Avoid vague steps like "handle errors" or "make sure it works".

Every step is an action. Don't add a step for summarizing or reporting — the harness asks you for the final summary automatically when the plan completes.

Order steps investigate → change → verify, and make verification its own step.

Don't write the plan as prose. If the task is trivial or one step, don't propose a plan.
