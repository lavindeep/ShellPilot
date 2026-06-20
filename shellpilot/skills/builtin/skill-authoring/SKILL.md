---
name: skill-authoring
description: Guidance for writing ShellPilot skills.
---
A skill is a folder whose name is the skill name. Its SKILL.md holds frontmatter (`name`, `description` only) and a body injected verbatim when the skill is active. Keep the body small — it spends context on every active turn — and push depth into references the model reads on demand.

The detail for that lives in three on-demand references. Open one with `skill_read(skill="skill-authoring", resource="<name>")`:

- `skill-anatomy` — read when laying out a skill folder: SKILL.md format, the injected-body token budget, references vs templates vs scripts, path safety and caps.
- `trigger-writing` — read when choosing when a skill activates: each trigger and when it fires, picking the narrowest one, why user skills are opt-in.
- `resource-routing` — read when deciding what goes in the body vs references vs templates, and how to route from the body to on-demand docs by name.

This skill is the pattern in miniature: a lean body that routes to named references rather than inlining everything.
