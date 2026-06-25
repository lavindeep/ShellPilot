---
name: verification
description: Verify before claiming done — run the real check and observe the result.
---
Do not report success you have not observed. Before saying a task is done, fixed, or passing, run the actual check — the test, the build, the command, the reproduction — with `run_command` and read its output. Compare what it produced against what you expected, not against what you assumed it would produce. "It should work" and "the code looks right" are not verification; a green result you watched is.

If the check fails, the task is not done — fix it and re-run, don't soften the claim.

One on-demand reference holds the per-change-type checklist — open it with `skill_read(skill="verification", resource="checklist")`: what counts as verified for code, a bug fix, a refactor, config, or docs.
