"""Model-name completion for `/model use` arguments."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from shellpilot.config.model import is_tested_model
from shellpilot.llm.ollama import LocalModel


@dataclass(frozen=True)
class ModelCompletionMatch:
    label: str
    fill: str
    hint: str


_MODEL_USE_PREFIX = "/model use "


def model_completion_matches(
    text: str, models: list[LocalModel], *, limit: int = 20
) -> list[ModelCompletionMatch]:
    lowered = text.lower()
    if not lowered.startswith(_MODEL_USE_PREFIX):
        return []
    raw_prefix = text[len(_MODEL_USE_PREFIX) :]
    if "\n" in raw_prefix:
        return []
    try:
        parsed = shlex.split(raw_prefix)
    except ValueError:
        parsed = []
    name_prefix = parsed[0] if len(parsed) == 1 else raw_prefix

    matches: list[ModelCompletionMatch] = []
    for model in models:
        if not model.name.startswith(name_prefix):
            continue
        hint = f"{model.size_bytes / 1e9:.1f} GB"
        if not is_tested_model(model.name):
            hint += " untested"
        matches.append(
            ModelCompletionMatch(
                label=model.name,
                fill=_MODEL_USE_PREFIX + _escape_model_name(model.name),
                hint=hint,
            )
        )
        if len(matches) >= limit:
            break
    return matches


def _escape_model_name(name: str) -> str:
    return name.replace("\\", "\\\\").replace(" ", "\\ ")
