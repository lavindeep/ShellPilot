# Common Debugging Traps

The patterns that turn a ten-minute fix into an hour. Watch for these in your own process.

## Fixing the symptom, not the cause

The error surfaces in one place but originates in another. Swallowing an exception, clamping a bad value, or adding a null check where it crashes hides the failure without removing it — it reappears under a slightly different input. Trace back to where the wrong value was produced and fix it there.

## Changing several things at once

If you edit three things and the failure clears, you don't know which one mattered — and two of them may be new latent bugs. Change one thing, re-test, then the next. One variable per experiment.

## Trusting an unconfirmed cause

"It must be the cache" is a hypothesis, not a finding. Acting on a guess you haven't isolated wastes the edit and muddies the next diagnosis. Confirm the cause with a probe or a bisect before you fix it.

## Not reproducing first

Editing toward a failure you can't trigger means you can't tell when it's fixed. Get a reliable reproduction before touching code.

## Assuming instead of reading

"This function returns a list" — does it? Read the source with `read_file` and check the actual value with a probe rather than the value you expect. Most stubborn bugs live in the gap between assumption and reality.

## Skipping verification

A change that looks right is not a confirmed fix. Re-run the reproduction and watch it pass before claiming done.

## Widening scope mid-debug

Refactoring "while you're in there" mixes unrelated changes into the fix, making it harder to review and to bisect later. Fix the bug; note the cleanup separately.
