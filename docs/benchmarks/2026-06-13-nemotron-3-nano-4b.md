# Phase 0.5 Model Capability Benchmark — nemotron-3-nano:4b

Date: 2026-06-13
Script: `scripts/benchmark_model.py` (standalone, live Ollama; never run in CI)
Settings: `num_ctx=8192`, `temperature=0.2`, Ollama current, macOS (Darwin 25.6.0)
Model: NVIDIA Nemotron 3 Nano 4B (3.97B, hybrid Mamba-Transformer), Q4_K_M (2.8 GB)

This benchmark gates the section 12.5 edit strategy per design section 27.2:
if exact-span reproduction fails more than ~20% of the time, anchored
`replace_exact`-style edits must be replaced with line-window edits or
small-file whole rewrites.

## Results

### a) Tool-call format reliability (10 trials per tool)

| Tool | Well-formed rate |
|---|---|
| `read_file` | 10/10 |
| `list_dir` | 10/10 |
| `search_text` | 10/10 |
| `write_file` | 10/10 |
| `patch_file` | 10/10 |
| `run_command` | 10/10 |
| `env_info` | 10/10 |

Well-formed means: exactly one tool call, correct tool name, all required
arguments present with correct JSON types. Nemotron matches gemma4:e4b and
qwen3.5:4b-mlx here — flat-schema tool calling is solid.

### b) Exact-span reproduction (15 trials over 3 targets)

**0/15 — total failure.** Every attempt was a *content* mismatch (not a
whitespace-trim near-miss): the model rewrote/reformatted the function body in
the `old` argument instead of copying it byte-for-byte from the file. The
one-retry whitespace-trim rescue cannot recover a content mismatch.

This is a meaningful regression versus gemma4:e4b (15/15 byte-exact) and
qwen3.5:4b-mlx (15/15, one whitespace rescue). The likely cause is the model's
reasoning/"improve the code" instinct: it does not treat the span as opaque
text to reproduce. Reasoning-on mode would probably make this *worse*, not
better, for byte-exact reproduction.

### c) Multi-turn chaining (5 chains × 6 steps)

**0/5 chains fully intact.** Per-step rates: `[1.0, 1.0, 1.0, 1.0, 1.0, 0.0]`.
Step 5 rate: 1.0. Steps 1–5 are flawless, but the model fails the 6th (final)
step **every single time** — a dead-consistent off-by-one where it wraps up one
step early instead of making the last tool call. For step-by-step plan
execution this means it would skip the final step of a plan.

### d) Stopping behavior (10 trials each)

- Plain question with all 7 tools offered: answered in text without tool calls 10/10.
- After a completed 1-tool task: produced a final text answer (no extra calls) 10/10.

## Gate decision

**Exact-span failure rate: 100% (threshold: ~20%). nemotron-3-nano:4b FAILS the
section 12.5 gate.** With the current anchored-edit strategy, `patch_file`
edits would not work on this model — it would require line-window edits or
small-file whole rewrites, which ShellPilot does not currently ship.

**Not recommended as a daily-driver model.** It ties gemma4:e4b on the easy axes
(tool-call format, stopping) but craters on the two that matter for this harness
(byte-exact editing and full plan-step execution). gemma4:e4b remains the
recommended model.

## Notes

- The public leaderboard numbers (strong BFCLv3 / IFEval) did not predict either
  failure — confirming that in-harness behavior on this benchmark, not generic
  agentic benchmarks, is the decision signal.
- The two failing axes (exact-span, chaining) are exactly where this benchmark
  *discriminates* between capable 4B models; it is not saturated.

## Caveats

- Temperature 0.2 and a small synthetic corpus (one ~30-line file, three span
  targets). Real-world files are longer and noisier; the read-before-write
  snapshot validation and one-retry recovery loop (sections 10.4, 12.4) remain
  load-bearing regardless of these scores.
- Reasoning-off only. NVIDIA's recommended tool-calling params (temp 0.6,
  top_p 0.95) and reasoning-on mode were not measured here; a fair first
  comparison matches the gemma4:e4b methodology (temp 0.2, reasoning-off).
- `num_ctx=8192`; long-context degradation was not measured here.
- Raw JSON: `docs/benchmarks/2026-06-13-nemotron-3-nano-4b.json`
- Reproducible via: `python scripts/benchmark_model.py --model nemotron-3-nano:4b --trials 10`
