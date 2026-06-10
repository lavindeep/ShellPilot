"""Prompts used during task execution (design sections 13.4, 18.3)."""

from __future__ import annotations

EXPLAINER_PROMPT = """\
You are explaining a risky command to the user before they decide whether to run it.
In one or two short sentences, state why this command is being run for the current
task and what effect it will have. Be specific and honest about destructive effects.
Do not soften or downplay the risk. Reply with the explanation only.

Command: {command}
Working directory: {cwd}
Risk flags: {reasons}
Current task context: {context}"""
