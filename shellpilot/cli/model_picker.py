"""Boot model picker for interactive sessions (design section 32).

Pure functions that choose and display a model from the list of installed
Ollama models. Console I/O is injected so every public function is unit-testable
without patching stdin.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from shellpilot.config.model import is_tested_model
from shellpilot.llm.ollama import LocalModel


def should_show_picker(
    *,
    tty: bool,
    model_override: str | None,
    installed_count: int,
) -> bool:
    """Return True when the interactive picker should be presented to the user.

    Skips the picker when:
    - *model_override* is set (the user passed ``--model``),
    - the session is non-interactive (*tty* is False),
    - only one model is installed (no real choice to make).
    """
    if model_override is not None:
        return False
    if not tty:
        return False
    if installed_count <= 1:
        return False
    return True


def resolve_preselect(
    config_default: str,
    last_model: str | None,
    installed: set[str],
) -> str:
    """Choose which model row to highlight as the default.

    Prefers *last_model* when it is still present in *installed*; otherwise
    falls back to *config_default*.
    """
    if last_model is not None and last_model in installed:
        return last_model
    return config_default


def choose_model(console: Console, models: list[LocalModel], preselect: str) -> str:
    """Print a numbered model list and prompt the user to pick one.

    Accepted input:
    - Empty (Enter) — returns *preselect*.
    - A row number (1-based) — returns the model at that index.
    - An exact model name present in *models* — returns that name.

    Re-prompts on anything else. Returns *preselect* on EOFError or
    KeyboardInterrupt so non-interactive fall-through is always safe.
    """
    name_to_index: dict[str, int] = {}
    console.print()
    for i, model in enumerate(models, 1):
        name_to_index[model.name] = i
        size_gb = model.size_bytes / 1_073_741_824
        size_str = f"{size_gb:.1f} GB"
        marker = "[sp.chevron]❯[/sp.chevron]" if model.name == preselect else " "
        name_part = f"[sp.emph]{escape(model.name)}[/sp.emph]"
        if not is_tested_model(model.name):
            tag = "  [sp.dim]untested[/sp.dim]"
        else:
            tag = ""
        console.print(f"  {marker} {i}.  {name_part}  [sp.dim]{size_str}[/sp.dim]{tag}")
    console.print()

    prompt = f"Select a model [sp.dim]\\[Enter = {escape(preselect)}][/sp.dim] "
    count = len(models)
    while True:
        try:
            raw = console.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return preselect

        if raw == "":
            return preselect

        # Numeric row
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= count:
                return models[index - 1].name

        # Exact model name
        if raw in name_to_index:
            return raw
