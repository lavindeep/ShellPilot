# Staging and Commit Messages

## Inspect first

Before staging, run `git status` to see what changed and `git diff` to read the actual content. Read it — this is where you catch a debugging print, a secret, an unintended file, or a half-finished edit before it enters history.

## Stage deliberately

Add only the files that belong to this change. `git add <path>` over `git add -A` when the working tree holds unrelated edits, so each commit stays one logical change. Review staged content with `git diff --staged` before committing — the staged set is what gets recorded, not the working tree.

## Atomic commits

One commit, one logical change. A commit that fixes a bug *and* renames a module *and* tweaks formatting is hard to review, hard to revert, and useless to `git bisect`. If you did several things, split them. Each commit should leave the tree in a working state.

## Writing the message

- **Subject line:** imperative mood, ~50 characters, no trailing period — "Fix off-by-one in pager", not "Fixed the pager bug.". It completes the sentence "If applied, this commit will…".
- **Body** (after a blank line, wrapped ~72 cols): explain *why* the change was made and what it affects, not a line-by-line restatement of the diff — the diff already shows what. Note non-obvious consequences or trade-offs.
- Reference the issue or context if there is one.

## Conventional commits

Many projects prefix the subject with a type: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Match the repository's existing style — read recent `git log` before inventing a format.
