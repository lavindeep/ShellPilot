---
name: code-review
description: Review a change across correctness, security, clarity, tests, and scope.
---
Review a change against the work it was meant to do, then across fixed dimensions so nothing is skipped: correctness (does it do what's asked, including edge cases), security (untrusted input, secrets, unsafe calls), clarity (will the next reader follow it), tests (is the new behavior actually covered), and scope (does the diff do only what was asked, no unrelated changes). Read the diff with `run_command` (`git diff`), the surrounding code with `read_file`, and find related call sites with `search_text` — a change is only correct in context.

Be specific: name the file, the line, and the concrete risk, not a vague concern.

One on-demand reference gives concrete prompts per dimension — open it with `skill_read(skill="code-review", resource="dimensions")`.
