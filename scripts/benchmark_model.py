#!/usr/bin/env python3
"""Phase 0.5 model capability benchmark (docs/DESIGN.md section 27.2).

Measures, against a live local Ollama model:

  a) tool-call format reliability per v1 tool
  b) exact-span reproduction (predicts anchored-edit viability)
  c) multi-turn chaining quality at step 5+
  d) stopping behavior (no tool calls once the task is done / for chat)

Standalone by design: this script talks to Ollama directly and is NOT part of
the test suite. CI never runs it. Results gate the section 12.5 edit strategy.

Usage:
    python scripts/benchmark_model.py [--model gemma4:e4b] [--trials 10] [--quick]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_MODEL = "gemma4:e4b"
DEFAULT_BASE_URL = "http://localhost:11434"
NUM_CTX = 8192
TEMPERATURE = 0.2
REQUEST_TIMEOUT = 120.0

SYSTEM_PROMPT = (
    "You are a local coding assistant running in a terminal harness. "
    "When the user asks for an action, call the provided tool with correct arguments. "
    "When no action is needed, answer in plain text without calling tools."
)


def tool_spec(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


@dataclass(frozen=True)
class ToolScenario:
    spec: dict[str, Any]
    prompt: str
    required_args: dict[str, type]

    @property
    def name(self) -> str:
        return str(self.spec["function"]["name"])


SCENARIOS: list[ToolScenario] = [
    ToolScenario(
        tool_spec(
            "read_file",
            "Read the contents of a file.",
            {"path": {"type": "string", "description": "File path to read."}},
            ["path"],
        ),
        "Read the file src/config.py and show me its contents.",
        {"path": str},
    ),
    ToolScenario(
        tool_spec(
            "list_dir",
            "List the entries of a directory.",
            {"path": {"type": "string", "description": "Directory path to list."}},
            ["path"],
        ),
        "List the files in the tests directory.",
        {"path": str},
    ),
    ToolScenario(
        tool_spec(
            "search_text",
            "Search project files for a text pattern.",
            {
                "pattern": {"type": "string", "description": "Text pattern to search for."},
                "path": {"type": "string", "description": "Directory to search in."},
            },
            ["pattern", "path"],
        ),
        "Search the project directory . for the pattern load_config.",
        {"pattern": str, "path": str},
    ),
    ToolScenario(
        tool_spec(
            "write_file",
            "Create or overwrite a file with the given content.",
            {
                "path": {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "Full file content."},
            },
            ["path", "content"],
        ),
        "Create a new file named notes.txt containing the single line: hello world",
        {"path": str, "content": str},
    ),
    ToolScenario(
        tool_spec(
            "patch_file",
            "Replace one exact text block in a file with a new text block.",
            {
                "path": {"type": "string", "description": "File path to edit."},
                "old": {"type": "string", "description": "Exact existing text to replace."},
                "new": {"type": "string", "description": "Replacement text."},
            },
            ["path", "old", "new"],
        ),
        "In the file app.py, replace the text DEBUG = False with DEBUG = True.",
        {"path": str, "old": str, "new": str},
    ),
    ToolScenario(
        tool_spec(
            "run_command",
            "Run a command without a shell, as an argv list.",
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command and arguments, e.g. ['pytest', '-q'].",
                }
            },
            ["argv"],
        ),
        "Run the test suite with pytest.",
        {"argv": list},
    ),
    ToolScenario(
        tool_spec(
            "env_info",
            "Report the operating system, working directory, and environment summary.",
            {},
            [],
        ),
        "What operating system is this machine running? Check with the env_info tool.",
        {},
    ),
]

# Exact-span targets: realistic code with indentation, blank lines, quotes,
# and operators. The model must reproduce the block byte-exactly in `old`.
SPAN_FILE = '''\
"""Order processing helpers."""

import logging

logger = logging.getLogger(__name__)

TAX_RATE = 0.0825


def subtotal(items: list[dict]) -> float:
    total = 0.0
    for item in items:
        total += item["price"] * item["quantity"]
    return total


def apply_discount(amount: float, code: str | None) -> float:
    if code is None:
        return amount

    if code == "SAVE10":
        return amount * 0.90
    logger.warning("unknown discount code: %s", code)
    return amount


def total_due(items: list[dict], code: str | None = None) -> float:
    amount = subtotal(items)
    amount = apply_discount(amount, code)
    return round(amount * (1 + TAX_RATE), 2)
'''

SPAN_TARGETS = {
    "subtotal": (
        "def subtotal(items: list[dict]) -> float:\n"
        "    total = 0.0\n"
        "    for item in items:\n"
        '        total += item["price"] * item["quantity"]\n'
        "    return total"
    ),
    "apply_discount": (
        "def apply_discount(amount: float, code: str | None) -> float:\n"
        "    if code is None:\n"
        "        return amount\n"
        "\n"
        '    if code == "SAVE10":\n'
        "        return amount * 0.90\n"
        '    logger.warning("unknown discount code: %s", code)\n'
        "    return amount"
    ),
    "total_due": (
        "def total_due(items: list[dict], code: str | None = None) -> float:\n"
        "    amount = subtotal(items)\n"
        "    amount = apply_discount(amount, code)\n"
        "    return round(amount * (1 + TAX_RATE), 2)"
    ),
}

CHAIN_FILES = [f"src/step{i}.py" for i in range(1, 7)]


class Bench:
    def __init__(self, base_url: str, model: str) -> None:
        self.model = model
        self.client = httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT)

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE},
        }
        if tools:
            payload["tools"] = tools
        response = self.client.post("/api/chat", json=payload)
        response.raise_for_status()
        message: dict[str, Any] = response.json()["message"]
        return message

    @staticmethod
    def extract_call(message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        calls = message.get("tool_calls") or []
        if len(calls) != 1:
            return None
        function = calls[0].get("function") or {}
        name = function.get("name")
        args = function.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None
        if not isinstance(name, str) or not isinstance(args, dict):
            return None
        return name, args

    # -- a) tool-call format reliability ------------------------------------
    def bench_tool_format(self, trials: int) -> dict[str, float]:
        rates: dict[str, float] = {}
        for scenario in SCENARIOS:
            ok = 0
            for _ in range(trials):
                message = self.chat(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": scenario.prompt},
                    ],
                    tools=[scenario.spec],
                )
                call = self.extract_call(message)
                if call is None:
                    continue
                name, args = call
                if name != scenario.name:
                    continue
                if all(
                    isinstance(args.get(arg), kind) and args.get(arg) not in ("", [], None)
                    for arg, kind in scenario.required_args.items()
                ):
                    ok += 1
            rates[scenario.name] = ok / trials
            print(f"  tool-format {scenario.name}: {ok}/{trials}", flush=True)
        return rates

    # -- b) exact-span reproduction ------------------------------------------
    def bench_exact_span(self, trials_per_target: int) -> dict[str, Any]:
        patch_spec = SCENARIOS[4].spec
        ok = 0
        total = 0
        near_misses: list[str] = []
        for target_name, expected in SPAN_TARGETS.items():
            for _ in range(trials_per_target):
                total += 1
                prompt = (
                    "Here is the current content of orders.py:\n\n"
                    f"```python\n{SPAN_FILE}```\n\n"
                    f"Rename the function `{target_name}` to `{target_name}_v2` using patch_file. "
                    f"Set `old` to the complete current definition of `{target_name}` copied "
                    "byte-for-byte from the file above (every space, blank line, and quote "
                    "exactly as shown), and `new` to the same text with only the function "
                    "name changed."
                )
                message = self.chat(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    tools=[patch_spec],
                )
                call = self.extract_call(message)
                if call is None or call[0] != "patch_file":
                    near_misses.append("no well-formed patch_file call")
                    continue
                old = call[1].get("old")
                if old == expected:
                    ok += 1
                elif isinstance(old, str) and old.strip() == expected.strip():
                    ok += 1  # leading/trailing whitespace only; recoverable by anchor trim
                    near_misses.append(f"{target_name}: whitespace-trim match")
                else:
                    near_misses.append(f"{target_name}: content mismatch")
            print(f"  exact-span {target_name}: running {ok}/{total}", flush=True)
        return {
            "rate": ok / total if total else 0.0,
            "ok": ok,
            "total": total,
            "notes": near_misses[:10],
        }

    # -- c) multi-turn chaining ------------------------------------------------
    def bench_chaining(self, chains: int) -> dict[str, Any]:
        read_spec = SCENARIOS[0].spec
        per_step_ok = [0] * len(CHAIN_FILES)
        completed = 0
        for _ in range(chains):
            file_list = ", ".join(CHAIN_FILES)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Read these files one at a time, in this exact order: {file_list}. "
                        "Make one read_file call, wait for the result, then call the next. "
                        "After the last file, summarize what you read in plain text."
                    ),
                },
            ]
            intact = True
            for step, expected_path in enumerate(CHAIN_FILES):
                message = self.chat(messages, tools=[read_spec])
                call = self.extract_call(message)
                if call is None or call[0] != "read_file" or call[1].get("path") != expected_path:
                    intact = False
                    break
                per_step_ok[step] += 1
                messages.append(
                    {"role": "assistant", "content": "", "tool_calls": message.get("tool_calls")}
                )
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {"path": expected_path, "content": f"value_{step} = {step}"}
                        ),
                    }
                )
            if intact:
                completed += 1
        print(f"  chains fully intact: {completed}/{chains}", flush=True)
        return {
            "chains": chains,
            "completed": completed,
            "per_step_rate": [count / chains for count in per_step_ok],
            "step5_rate": per_step_ok[4] / chains,
        }

    # -- d) stopping behavior ---------------------------------------------------
    def bench_stopping(self, trials: int) -> dict[str, float]:
        all_tools = [scenario.spec for scenario in SCENARIOS]
        read_spec = SCENARIOS[0].spec

        chat_no_tool = 0
        for _ in range(trials):
            message = self.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "What does the acronym CLI stand for?"},
                ],
                tools=all_tools,
            )
            if not message.get("tool_calls"):
                chat_no_tool += 1

        done_stops = 0
        for _ in range(trials):
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Read src/main.py and tell me what it does."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "src/main.py"}}}
                    ],
                },
                {
                    "role": "tool",
                    "content": json.dumps(
                        {"path": "src/main.py", "content": 'print("hello world")'}
                    ),
                },
            ]
            message = self.chat(messages, tools=[read_spec])
            if not message.get("tool_calls") and (message.get("content") or "").strip():
                done_stops += 1

        print(f"  chat answered without tools: {chat_no_tool}/{trials}", flush=True)
        print(f"  stopped after task completion: {done_stops}/{trials}", flush=True)
        return {
            "chat_no_tool_rate": chat_no_tool / trials,
            "stop_when_done_rate": done_stops / trials,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--quick", action="store_true", help="2 trials everywhere (smoke test)")
    parser.add_argument("--out", default="benchmark_results.json")
    args = parser.parse_args()

    trials = 2 if args.quick else args.trials
    bench = Bench(args.base_url, args.model)

    print(f"Benchmarking {args.model} (trials={trials}) ...", flush=True)
    print("[a] tool-call format reliability", flush=True)
    tool_format = bench.bench_tool_format(trials)
    print("[b] exact-span reproduction", flush=True)
    exact_span = bench.bench_exact_span(max(2, trials // 2))
    print("[c] multi-turn chaining", flush=True)
    chaining = bench.bench_chaining(max(2, trials // 2))
    print("[d] stopping behavior", flush=True)
    stopping = bench.bench_stopping(trials)

    results = {
        "model": args.model,
        "trials": trials,
        "num_ctx": NUM_CTX,
        "temperature": TEMPERATURE,
        "tool_format": tool_format,
        "exact_span": exact_span,
        "chaining": chaining,
        "stopping": stopping,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"\nResults written to {args.out}")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
