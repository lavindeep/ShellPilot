# Undoing Safely

Git rarely loses committed work — the trick is knowing the right tool for the situation and preferring the non-destructive one. In ShellPilot, `git reset` and `git clean` are HIGH-risk and require explicit approval; treat that as a signal to be sure before you run them.

## See where you've been: reflog

`git reflog` lists every position HEAD has held — commits, resets, rebases, even "lost" ones. If you reset or rebased and want the old state back, find its hash in the reflog and recover it. This is the safety net behind most "I lost my commits" situations; check it before assuming work is gone.

## Undo a commit without losing the work

- `git revert <commit>` — creates a *new* commit that undoes an earlier one. Safe on shared history because it adds rather than rewrites. Prefer this for anything already pushed.
- `git reset --soft HEAD~1` — moves the branch back one commit but keeps the changes staged. Good for "I committed too early".
- `git reset HEAD~1` (mixed, the default) — moves back one commit and unstages, keeping the file changes in the working tree.

## Destructive — be sure first

- `git reset --hard <ref>` — discards commits *and* working-tree changes back to `<ref>`. Lost working changes are not in the reflog. Confirm `git status` is what you expect first.
- `git clean -fd` — deletes untracked files and directories permanently. Run `git clean -nd` first to preview exactly what would be removed.

## Discard changes to a file

- `git restore <path>` — discard unstaged changes to a tracked file (working tree back to the index).
- `git restore --staged <path>` — unstage a file, keeping its changes in the working tree.

## Never rewrite shared history

`git reset`, `git rebase`, `git commit --amend`, and force-push rewrite commits. That is fine on a local branch only you have. Once commits are pushed and others may have pulled them, rewriting forces everyone into a painful reconciliation — use `git revert` instead. ShellPilot classifies force-push as HIGH-risk for exactly this reason.
