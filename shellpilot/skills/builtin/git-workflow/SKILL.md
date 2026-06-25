---
name: git-workflow
description: Safe git practice — inspect before committing, atomic commits, clear history.
---
Inspect before you commit: run `git status` and `git diff` and read them, so you commit what you intend and nothing stray. Keep commits atomic — one logical change each — with a message saying what changed and why. Work on a branch, and never rewrite history others may have pulled.

In ShellPilot, `git reset`, `git clean`, branch deletion, and force-push are HIGH-risk and need explicit approval; a plain `git push` is medium. That gate is a backstop, not a substitute for checking the diff yourself.

Two on-demand references — open with `skill_read(skill="git-workflow", resource="<name>")`:

- `commits` — staging deliberately and writing clear, atomic commit messages.
- `recovery` — undoing safely with reflog, reset, and restore.
