"""Slash commands: user controls for the harness itself (design section 20.1)."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shellpilot.config.loader import ConfigError, LoadedConfig
from shellpilot.llm.client import LLMClient
from shellpilot.runtime.conversation import ConversationRuntime


class SlashAction(Enum):
    CONTINUE = "continue"
    EXIT = "exit"


HELP_ROWS: list[tuple[str, str]] = [
    ("/help", "Show available commands."),
    ("/exit, /quit", "Exit ShellPilot."),
    ("/clear", "Clear the visible conversation after confirmation."),
    ("/status", "Show model, profile, workspace, and context usage."),
    ("/model", "Show the active model and context metadata."),
    ("/model list", "List local models (Gemma 4 family by default)."),
    ("/model use <name>", "Switch the active local model."),
    ("/config show", "Print resolved config with source layers."),
    ("/config edit", "Show the user config path for editing."),
    ("/config reload", "Reload config from disk."),
    ("/compact", "Truncate older conversation context now."),
    ("/compact status", "Show context usage and compaction thresholds."),
]


def render_config(loaded: LoadedConfig, console: Console) -> None:
    table = Table(title="Resolved configuration")
    table.add_column("Key")
    table.add_column("Value", overflow="fold")
    table.add_column("Source")
    for key, source in sorted(loaded.sources.items()):
        section, _, name = key.partition(".")
        value = getattr(getattr(loaded.settings, section), name)
        table.add_row(key, repr(value), source)
    console.print(table)


def _default_confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


class SlashDispatcher:
    """Parses and executes slash commands against the running session."""

    def __init__(
        self,
        *,
        runtime: ConversationRuntime,
        client: LLMClient,
        console: Console,
        loaded: LoadedConfig,
        user_config_file: Path,
        reload_config: Callable[[], LoadedConfig],
        confirm: Callable[[str], bool] = _default_confirm,
    ) -> None:
        self._runtime = runtime
        self._client = client
        self._console = console
        self._loaded = loaded
        self._user_config_file = user_config_file
        self._reload_config = reload_config
        self._confirm = confirm

    def handle(self, line: str) -> SlashAction:
        parts = line.strip().split()
        command, args = parts[0].lower(), parts[1:]

        if command in ("/exit", "/quit"):
            return SlashAction.EXIT
        if command == "/help":
            self._help()
        elif command == "/clear":
            self._clear()
        elif command == "/status":
            self._status()
        elif command == "/model":
            self._model(args)
        elif command == "/config":
            self._config(args)
        elif command == "/compact":
            self._compact(args)
        else:
            self._console.print(f"[red]Unknown command: {command}[/red] — type /help for commands.")
        return SlashAction.CONTINUE

    def _help(self) -> None:
        table = Table(title="ShellPilot commands")
        table.add_column("Command")
        table.add_column("Purpose")
        for command, purpose in HELP_ROWS:
            table.add_row(command, purpose)
        self._console.print(table)

    def _clear(self) -> None:
        if self._confirm("Clear the conversation?"):
            self._runtime.clear_history()
            self._console.print("[dim]Conversation cleared.[/dim]")

    def _status(self) -> None:
        status = self._runtime.status()
        self._console.print(f"Model: {status.model}")
        self._console.print(f"Profile: {status.profile}")
        self._console.print(f"Workspace: {status.workspace}")
        self._console.print(
            f"Context: ~{status.estimated_prompt_tokens} of "
            f"{status.budget.model_context_tokens} tokens "
            f"({status.history_messages} messages)"
        )
        self._console.print("Active plan: none")
        self._console.print("Pending approvals: none")

    def _model(self, args: list[str]) -> None:
        if not args:
            status = self._runtime.status()
            self._console.print(
                f"Active model: {status.model} "
                f"(context {status.budget.model_context_tokens} tokens)"
            )
            return
        if args[0] == "list":
            family = self._runtime.settings.model.family
            models = self._client.list_models()
            table = Table(title=f"Local models ({family} family)")
            table.add_column("Name")
            table.add_column("Size")
            shown = 0
            for model in models:
                if not model.name.startswith(family):
                    continue
                table.add_row(model.name, f"{model.size_bytes / 1e9:.1f} GB")
                shown += 1
            if shown:
                self._console.print(table)
            else:
                self._console.print(
                    f"[red]No {family} models installed.[/red] Try: ollama pull gemma4:e4b"
                )
            return
        if args[0] == "use" and len(args) > 1:
            name = args[1]
            installed = {model.name for model in self._client.list_models()}
            if name not in installed:
                self._console.print(f"[red]{name} is not installed.[/red] See /model list.")
                return
            if not name.startswith(self._runtime.settings.model.family):
                self._console.print(
                    f"[yellow]Warning: {name} is outside the supported "
                    f"{self._runtime.settings.model.family} family.[/yellow]"
                )
            self._runtime.set_model(name)
            self._console.print(f"Switched to {name}.")
            return
        self._console.print("Usage: /model | /model list | /model use <name>")

    def _config(self, args: list[str]) -> None:
        action = args[0] if args else "show"
        if action == "show":
            render_config(self._loaded, self._console)
        elif action == "edit":
            self._console.print(f"User config: {self._user_config_file}")
            self._console.print("Edit the file, then run /config reload.")
        elif action == "reload":
            try:
                self._loaded = self._reload_config()
            except ConfigError as exc:
                self._console.print(f"[red]Config reload failed:[/red] {exc}")
                return
            self._runtime.update_settings(self._loaded.settings)
            self._console.print("[dim]Config reloaded.[/dim]")
        else:
            self._console.print("Usage: /config show | /config edit | /config reload")

    def _compact(self, args: list[str]) -> None:
        if args and args[0] == "status":
            status = self._runtime.status()
            budget = status.budget
            self._console.print(f"Model: {status.model}")
            self._console.print(f"Detected context: {budget.model_context_tokens} tokens")
            self._console.print(f"Current prompt estimate: {status.estimated_prompt_tokens} tokens")
            self._console.print(f"Compact at: {budget.compact_at_tokens} tokens")
            self._console.print(f"Hard limit: {budget.hard_limit_tokens} tokens")
            self._console.print("Automatic compaction: on")
            self._console.print("Active plan: no")
            return
        dropped = self._runtime.compact_now()
        if dropped:
            self._console.print(f"[dim]Compacted: dropped {dropped} oldest messages.[/dim]")
        else:
            self._console.print("[dim]Nothing to compact.[/dim]")
