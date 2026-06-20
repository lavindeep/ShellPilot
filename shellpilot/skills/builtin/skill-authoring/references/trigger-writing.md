# Trigger Writing

A trigger decides when a skill's body is injected into the system prompt. A skill is **active when any one of its triggers fires** — they are OR'd, not AND'd. Pick the narrowest set that matches when the model actually needs the guidance; an always-on skill spends context on every turn whether it is relevant or not.

## The triggers

- `ALWAYS_ON` — fires every turn. Use only for tiny, genuinely universal guidance that belongs in the prompt unconditionally.
- `ENABLED` — fires when the skill's name appears in `[skills] enabled` in `config.toml`. This is the opt-in path: the skill is dormant until the user lists it.
- `WEB_ENABLED` — fires when web tools (`web_search` and `web_fetch`) are registered in the runtime. Use for guidance that only matters once web access is on.
- `PLAN_PROPOSED` / `PLAN_ACTIVE` / `PLAN_BLOCKED` — each fires when the live plan's status exactly matches `proposed`, `active`, or `blocked`. Use for plan-mode guidance scoped to one phase.

## User skills

User skills always get `ENABLED` — you cannot choose a different trigger for them. A user skill therefore stays inactive until its folder name is added to `[skills] enabled`. That opt-in is deliberate: dropping a folder in the skills directory does not silently change the model's behavior.

Builtin skills are assigned triggers by folder name (for example, `web-grounding` gets `WEB_ENABLED`); that mapping lives in the harness, not in frontmatter. There is no frontmatter key for triggers.

## Per-reference triggers (planning only)

References are normally trigger-less, which makes them on-demand (see `resource-routing`). The one exception is the builtin `planning` skill: its `references/proposed.md`, `active.md`, and `blocked.md` are assigned matching plan-status triggers by filename, so the right phase's reference is injected alongside the body. This is a closed builtin mechanism — it is not available to user skills, and ordinary references have no trigger. Do not add frontmatter to a reference to try to give it one; it has no effect and is not how the mechanism works.
