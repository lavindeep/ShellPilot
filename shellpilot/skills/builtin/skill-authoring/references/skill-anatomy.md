# Skill Anatomy

A skill is a folder under the builtin root (`shellpilot/skills/builtin/`) or the user root (`<config_dir>/skills/`). The folder name is the authoritative skill name — a `name:` in frontmatter that disagrees is ignored with an advisory.

## SKILL.md

Required. It has frontmatter, then a body after the closing `---`:

```
---
name: my-skill
description: One short sentence on when this skill applies.
---
<body — injected verbatim into the system prompt when the skill is active>
```

Frontmatter is hand-parsed: it must open and close with a `---` line, hold `key: value` pairs, and recognize only `name` and `description`. Any other key, a line without a `:`, or a missing delimiter makes the whole skill invalid — it then shows in `/skills` but never injects. An empty body is valid.

The body is injected into the system prompt whenever the skill is active, so it competes with everything else for context. It is bounded to `min(800, ctx // 12)` tokens (`ctx` is the model's context window) and truncated silently if longer. Keep it small and push depth into references — see `resource-routing`.

## References, templates, scripts

Optional subfolders hold extra material:

- `references/` — `*.md` files with deeper guidance, checklists, or examples. Each becomes a resource.
- `templates/` — `*.md` reusable starting points (blank structures to copy).
- `scripts/` — files plus a `scripts/manifest.json`. Scripts are discovered and recorded, but **never executed** — runtime execution is deferred to a future safety release. A `scripts/` folder with files but no manifest is ignored with a warning.

References and templates are not injected with the body by default. They are discovered, listed in `/skills`, and readable on demand (see `resource-routing` and `trigger-writing`).

## What's injected vs discovered

- **Injected** when the skill is active: the SKILL.md body, plus any reference whose own trigger fires (a planning-only mechanism — see `trigger-writing`).
- **Discovered only:** templates, scripts, and every trigger-less reference. They cost no prompt budget until the model reads them.

## Path safety and caps

Discovery stays inside the skill folder and reads only direct `*.md` children of `references/` and `templates/` — nested directories and non-`.md` files are ignored. Each resource file is capped at 64 KB on read, then bounded again by the token cap; at most 16 references and 16 templates load per skill (extras are dropped after sorting, with a warning). For user skills, paths that resolve outside the folder (symlinks, aliases) are skipped rather than followed.
