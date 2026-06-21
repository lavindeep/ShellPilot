# Verification Checklist

What "verified" means depends on what you changed. In every case the rule is the same: run the real check and observe the result before claiming done — never infer success from the diff.

## New code or a feature

- Run the test suite (or the specific tests covering the change) and confirm they pass.
- Run the build / type-check / lint that the project uses, and read the output for new errors.
- Exercise the new path at least once — call the function, hit the endpoint, run the command — and check it does what was asked, not just that it doesn't crash.

## A bug fix

- Re-run the exact reproduction that triggered the bug and confirm the failure is gone. A fix verified only by "the code now looks correct" is not verified.
- Run the surrounding tests to confirm you didn't break an adjacent case.
- If there was no failing test, the fix is incomplete until one exists that fails before the change and passes after.

## A refactor

- The behavior must be unchanged, so the existing tests must still pass with no edits. If you had to change a test's expectations, it wasn't a pure refactor — re-examine.
- Run the full relevant test set, not a subset; refactors break things at a distance.

## Config or dependency change

- Start the program / run the affected command and confirm it loads with the new config or version, rather than assuming the file is syntactically fine.
- Watch for warnings, not just errors.

## Documentation

- Render or read the result as a user would: check that code samples run, commands are copy-pasteable, and links resolve. Read it through once for accuracy against the implementation it describes.

## Before you claim done

State what you ran and what you saw — "ran `pytest`, 908 passed" — not "tests should pass". Evidence you observed, every time.
