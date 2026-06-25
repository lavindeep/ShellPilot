# The Debugging Loop

A repeatable order of operations. Each step gates the next — don't skip ahead.

## 1. Reproduce

Find the smallest, most reliable way to trigger the failure, and confirm it triggers now. Run the failing command, test, or input with `run_command` and capture the exact error. If you cannot reproduce it, you cannot know when it is fixed — gather the conditions (inputs, environment, recent changes) until it fails on demand.

## 2. Hypothesize

State one concrete, falsifiable guess about the cause: "the index is off by one", "the config isn't loaded before this call". Base it on the actual error text and the code you have read, not on a hunch. One hypothesis at a time — if you have several, rank them and test the cheapest first.

## 3. Isolate

Prove or disprove the hypothesis before changing code:

- **Narrow the input** — strip the reproduction to the minimum that still fails. Each thing removed that doesn't change the failure is ruled out.
- **Bisect the change history** — if it worked before, find the commit that broke it. `git bisect` halves the search each step: mark a known-good and known-bad commit, test the midpoint it checks out, mark good or bad, repeat. The first bad commit names the change responsible.
- **Add a probe** — a log line, an assertion, or a `read_file` of intermediate state to confirm what the values actually are versus what you assumed.

## 4. Fix the cause

Change the root cause you isolated, not the symptom downstream of it. Make one change. A fix that suppresses the error message without addressing why it occurred will resurface elsewhere.

## 5. Verify

Re-run the exact reproduction from step 1 and confirm the failure is gone. Then run the surrounding tests or build to confirm you didn't break anything adjacent. A fix you haven't observed passing is a hypothesis, not a fix — see the `verification` skill.
