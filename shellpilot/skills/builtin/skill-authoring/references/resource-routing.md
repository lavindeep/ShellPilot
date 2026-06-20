# Resource Routing

A skill's material is split by how often it is needed and how it reaches the model. Routing it well keeps the always-injected part small while leaving depth one step away.

## Where each kind belongs

- **SKILL.md body** — small, stable rules that are relevant whenever the skill is active. This text is injected into the system prompt every active turn and counts against the context budget, so spend it only on what the model must always see. State the behavior, then point to where the detail lives.
- **References** — deeper guidance, checklists, examples, and edge cases that matter only sometimes. They are not injected with the body; they wait until the model reads them.
- **Templates** — reusable blank structures to copy (a starter `SKILL.md`, an evaluation form). Like references, they are discovered, not injected.

## On-demand resources

A resource with no trigger is **on-demand**: discovered and readable, but never injected. Every template and every reference is on-demand — the only exception is the builtin `planning` skill's phase references, which carry triggers (see `trigger-writing`). On-demand resources cost no prompt budget until something opens them.

The model opens one with the `skill_read` tool, addressed by **name** scoped to a skill — never a file path. The name is the file stem: a reference `references/trigger-writing.md` is `trigger-writing`. So `skill_read(skill="skill-authoring", resource="trigger-writing")` returns that document's text. A value that looks like a path (`references/foo.md`, `../secret`) matches nothing and fails cleanly. `skill_read` is read-only, with no approval gate.

When the user has opted into at least one skill, the harness injects a one-line **`Readable docs`** menu listing the on-demand resource names of every active skill. You do not maintain that list — it is generated from what is discovered. Authors do not have to advertise resource names by hand; the menu does it.

## The progressive-disclosure pattern

Keep SKILL.md lean and move depth into on-demand references, then route to them from the body **by name**: "read `trigger-writing` when choosing a trigger." The model sees the routing in the always-injected body, sees the name in the `Readable docs` menu, and pulls the full document with `skill_read` only when it is relevant. This skill is itself the example — a short body that hands off to three named references.
