---
name: debugging
description: Systematic debugging — reproduce, hypothesize, isolate, fix the cause, verify.
---
Debug by method, not by guessing. Reproduce the failure first — you cannot fix what you cannot trigger. Form one hypothesis about the cause, then isolate it (narrow the input, bisect the history, or add a probe) before editing. Fix the root cause, not the symptom, one change at a time. Then verify by re-running the exact reproduction.

Inspect with `read_file` and `search_text`; reproduce and re-check with `run_command`.

Two on-demand references — open with `skill_read(skill="debugging", resource="<name>")`:

- `method` — the full loop, with how to bisect and write a minimal reproduction.
- `common-traps` — patterns that waste time (symptom-fixing, changing several things at once, trusting an unconfirmed cause).
