# Review Dimensions

Concrete questions to ask per dimension. Read the diff in the context of the surrounding code — a line that is correct in isolation can be wrong in its caller.

## Correctness

- Does it actually do what the task asked, or only the easy part of it?
- Edge cases: empty input, zero, negative, very large, null/None, the first and last element, concurrent access.
- Off-by-one in loops and slices; boundary conditions on comparisons (`<` vs `<=`).
- Error paths: are failures handled, or swallowed silently? Are exceptions caught too broadly?
- Does it match the existing behavior it's supposed to extend, or quietly change it?

## Security

- Untrusted input reaching a shell, query, file path, or eval — is it validated and escaped?
- Secrets, keys, or tokens hardcoded, logged, or committed.
- Path traversal (`..`), symlink following, writing outside the intended directory.
- Permission and authorization checks present where state changes.
- New dependencies — are they needed, and from a trustworthy source?

## Clarity

- Will the next reader understand this without the author present? Names that say what, not how.
- Is complexity justified, or is there a simpler shape? Flag speculative abstraction.
- Comments explain *why*, not restate *what*. Dead code and leftover debugging removed.

## Tests

- Is the new behavior covered, including the edge cases above — not just the happy path?
- Would the tests actually fail if the change were wrong? A test that passes against a broken implementation tests nothing.
- For a bug fix: is there a regression test that fails before the fix?

## Scope

- Does the diff do only what was asked? Flag unrelated refactors, formatting churn, and "while I was here" changes — they hide the real change and complicate review and rollback.
- Are there now-dead branches or unused parameters left behind?

## Delivering the review

Lead with anything blocking (correctness, security). For each finding give file, line, and the specific risk or fix — "`parse.py:42` — `index` can be -1 when the list is empty, raising IndexError" beats "possible bug in parsing". Separate must-fix from nice-to-have.
