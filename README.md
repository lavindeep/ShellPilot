# ShellPilot

**A local-first AI shell harness for your terminal, powered by Ollama.**

ShellPilot gives you one terminal conversation that can answer questions, inspect your project, plan multi-step work, and run commands with risk-based approvals — all against a local model (Gemma 4 via [Ollama](https://ollama.com)). No cloud calls, no telemetry, no API keys. Your code and your shell never leave your machine.

## Status

🚧 **Pre-alpha.** The design is settled in [`docs/DESIGN.md`](docs/DESIGN.md) and the runtime is being built phase by phase:

- [x] Phase 0 — Foundation: package scaffold, tracked tests, CI, design doc
- [ ] Phase 0.5 — Model capability validation (benchmark against `gemma4:e4b`)
- [ ] Phase 1 — Local Ollama chat loop
- [ ] Phase 2 — Read-only tools (`read_file`, `list_dir`, `search_text`, `env_info`)
- [ ] Phase 3 — Planning and command execution (`shell=False`, deterministic risk policy)
- [ ] Phase 4 — File writes and anchored edits with read-before-write snapshots
- [ ] Phase 5 — Security profiles, dangerous-command approvals, audit logging

A demo recording of the full plan → approve → edit → verify loop lands here when v1 is complete.

## Install (development)

Requires Python 3.11+ and a local [Ollama](https://ollama.com) install with a Gemma 4 model (default: `gemma4:e4b`).

```bash
git clone https://github.com/lavindeep/ShellPilot.git
cd ShellPilot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
shellpilot
```

## Development checks

The same four checks CI runs:

```bash
ruff check .
ruff format --check .
mypy shellpilot --strict
pytest
```

CI never needs Ollama or a GPU — the runtime is tested against a fake model.

## Design highlights

- **Local only.** All model calls go through local Ollama; there is no telemetry and no remote logging.
- **One conversation loop.** No separate chat/agent modes — the runtime infers whether a turn is a question, an inspection, a plan, or an action.
- **Deterministic security first.** Command risk is classified by deterministic policy (executable, flags, targets, metacharacters) — never by asking the model whether something is safe. Agent commands run with `shell=False`.
- **Plans are artifacts.** Complex tasks get a visible, editable `PLAN.md` under `.shellpilot/tasks/<task-id>/`, approved before execution and updated as work progresses.
- **Read-before-write edits.** File edits are anchored patches validated against a content-hashed snapshot of what was actually read — no blind writes.
- **Testable without a model.** A fake LLM client exercises the whole runtime in CI, including malformed tool calls and stuck loops.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## License

[MIT](LICENSE)
