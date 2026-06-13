"""Slash commands: user controls for the harness itself (design section 20.1)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shellpilot.cli.attachments import AttachmentError, AttachmentQueue, load_image
from shellpilot.cli.render import plan_panel, render_diff
from shellpilot.cli.theme import UNICODE_GLYPHS, Glyphs
from shellpilot.config.loader import BOOT_ONLY_KEYS, ConfigError, LoadedConfig, validate_override
from shellpilot.config.overrides import load_overrides, overrides_path, save_overrides
from shellpilot.llm.client import LLMClient
from shellpilot.runtime.conversation import ConversationRuntime


class SlashAction(Enum):
    CONTINUE = "continue"
    EXIT = "exit"
    MANUAL_SHELL = "manual_shell"


HELP_ROWS: list[tuple[str, str]] = [
    ("/help", "Show available commands."),
    ("/exit, /quit", "Exit ShellPilot."),
    ("/clear", "Clear the visible conversation after confirmation."),
    ("/status", "Show model, profile, workspace, and context usage."),
    ("/model", "Show the active model and context metadata."),
    ("/model list", "List installed local models with tested/untested tags."),
    ("/model use <name>", "Switch the active local model."),
    ("/config show", "Print resolved config with source layers."),
    ("/config edit", "Show the user config path for editing."),
    ("/config reload", "Reload config from disk."),
    ("/config set <key> <value>", "Persist a runtime override to overrides.json."),
    ("/config unset <key>", "Remove a persisted override and revert to the underlying layer."),
    ("/config reset", "Clear all persisted overrides after confirmation."),
    ("/compact", "Compact older conversation context now."),
    ("/compact status", "Show context usage and compaction thresholds."),
    ("/compact auto <on|off>", "Toggle automatic token-budget compaction."),
    ("/context", "Show the per-block context breakdown with token estimates."),
    ("/plan", "Show the active plan."),
    ("/plan path", "Show the active plan artifact path."),
    ("/plan cancel", "Cancel the active plan after confirmation."),
    ("/plan revise <text>", "Ask the assistant to revise the active plan."),
    ("/cwd", "Show the workspace boundary."),
    ("/cwd set <path>", "Change the workspace boundary after confirmation."),
    ("/doctor", "Check Python, Ollama, models, and paths."),
    ("/tools", "List tools available under the active profile."),
    ("/diff", "Show diffs from this session's agent edits."),
    ("/profile", "Show the active security profile."),
    ("/profile use <name>", "Switch profile: supervised or balanced."),
    ("/logs", "Show recent audit events for this session."),
    ("/logs all", "Show recent audit events across all sessions."),
    ("/export <path>", "Export this session's transcript to markdown."),
    ("/memory show", "Show stored preferences and project facts with ids."),
    ("/memory add <text>", "Add a global behavior preference after confirmation."),
    ("/memory forget <id>", "Remove a memory entry after confirmation."),
    ("/memory compact", "Model-assisted preference cleanup, approved before saving."),
    ("/prefs show", "Show behavior preferences."),
    ("/prefs edit", "Show the memory file paths for hand-editing."),
    ("/shell", "Enter Manual Shell mode (raw shell, user-typed)."),
    ("/attach <path>", "Stage an image to send with your next message (vision models only)."),
    ("/attach", "List currently staged images."),
    ("/skills", "List discovered skills with triggers, resources, and active status."),
]


def command_words() -> list[str]:
    """Completion phrases derived from HELP_ROWS: split combined rows, drop <args>."""
    words: list[str] = []
    for entry, _ in HELP_ROWS:
        for raw in entry.split(","):
            phrase = " ".join(part for part in raw.split() if not part.startswith("<"))
            if phrase and phrase not in words:
                words.append(phrase)
    return words


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
        glyphs: Glyphs = UNICODE_GLYPHS,
        preload: Callable[[str], None] | None = None,
        attachments: AttachmentQueue | None = None,
    ) -> None:
        self._runtime = runtime
        self._client = client
        self._console = console
        self._loaded = loaded
        self._user_config_file = user_config_file
        self._reload_config = reload_config
        self._confirm = confirm
        self._glyphs = glyphs
        self._preload = preload
        self._attachments = attachments

    def handle(self, line: str) -> SlashAction:
        parts = line.strip().split()
        command, args = parts[0].lower(), parts[1:]

        if command in ("/exit", "/quit"):
            return SlashAction.EXIT
        if command == "/shell":
            return SlashAction.MANUAL_SHELL
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
        elif command == "/context":
            self._context()
        elif command == "/plan":
            self._plan(args)
        elif command == "/cwd":
            self._cwd(args)
        elif command == "/doctor":
            self._doctor()
        elif command == "/tools":
            self._tools()
        elif command == "/diff":
            self._diff()
        elif command == "/profile":
            self._profile(args)
        elif command == "/logs":
            self._logs(args)
        elif command == "/export":
            self._export(args)
        elif command == "/memory":
            self._memory(args)
        elif command == "/prefs":
            self._prefs(args)
        elif command == "/attach":
            self._attach(args)
        elif command == "/skills":
            self._skills()
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
        if self._confirm("Clear the conversation (also cancels the active plan)?"):
            had_plan = self._runtime.plan_manager.active is not None
            self._runtime.clear_history()
            if had_plan:
                self._console.print("[dim]Conversation cleared and active plan cancelled.[/dim]")
            else:
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
        plan = self._runtime.plan_manager.active
        if plan is not None:
            self._console.print(f"Active plan: {plan.task_id} ({plan.status})")
        else:
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
            from shellpilot.config.model import TESTED_FAMILIES, is_tested_model

            models = self._client.list_models()
            table = Table(title="Local models")
            table.add_column("Name")
            table.add_column("Size")
            table.add_column("Tag")
            for model in models:
                if is_tested_model(model.name):
                    tag = "[sp.accent]tested[/sp.accent]"
                else:
                    tag = "[sp.dim]untested[/sp.dim]"
                table.add_row(model.name, f"{model.size_bytes / 1e9:.1f} GB", tag)
            if models:
                self._console.print(table)
            else:
                self._console.print(
                    f"[red]No models installed.[/red] Try: ollama pull {TESTED_FAMILIES[0]}:e4b"
                )
            return
        if args[0] == "use" and len(args) > 1:
            from shellpilot.config.model import TESTED_FAMILIES, is_tested_model
            from shellpilot.persistence.workspace_state import save_last_model

            name = args[1]
            installed = {model.name for model in self._client.list_models()}
            if name not in installed:
                self._console.print(f"[red]{name} is not installed.[/red] See /model list.")
                return
            self._runtime.set_model(name)
            workspace = self._runtime.status().workspace
            try:
                save_last_model(workspace, name)
            except OSError as exc:
                self._console.print(f"[dim]Warning: could not save model choice: {exc}[/dim]")
            if self._preload is not None:
                self._preload(name)
            self._console.print(f"Switched to {name}.")
            if not is_tested_model(name):
                families = ", ".join(TESTED_FAMILIES)
                self._console.print(
                    f"[sp.dim]Note: {name} is not a tested family ({families}); "
                    "quality may vary. scripts/benchmark_model.py is the qualification "
                    "path.[/sp.dim]"
                )
            return
        self._console.print("Usage: /model | /model list | /model use <name>")

    def _config(self, args: list[str]) -> None:  # noqa: C901 - intentionally one handler
        action = args[0] if args else "show"
        if action == "show":
            render_config(self._loaded, self._console)
        elif action == "edit":
            self._console.print(f"User config: {self._user_config_file}")
            self._console.print("Edit the file, then run /config reload.")
            self._console.print(
                "[dim]Tip: use /config set <key> <value> "
                "to make persistent changes in-program.[/dim]"
            )
        elif action == "reload":
            try:
                self._loaded = self._reload_config()
            except ConfigError as exc:
                self._console.print(f"[red]Config reload failed:[/red] {exc}")
                return
            self._runtime.update_settings(self._loaded.settings)
            self._console.print("[dim]Config reloaded.[/dim]")
            self._print_config_warnings()
        elif action == "set":
            self._config_set(args[1:])
        elif action in ("unset", "reset") and len(args) > 1:
            self._config_unset(args[1])
        elif action == "reset" and len(args) == 1:
            self._config_reset_all()
        else:
            self._console.print(
                "Usage: /config show | /config edit | /config reload"
                " | /config set <key> <value>"
                " | /config unset <key>"
                " | /config reset [<key>]"
            )

    def _overrides_path(self) -> Path:
        return overrides_path(self._user_config_file.parent)

    def _print_config_warnings(self) -> None:
        for warning in self._loaded.warnings:
            self._console.print(f"[dim]{warning}[/dim]")

    @staticmethod
    def _resolve_setting_value(loaded: LoadedConfig, key: str) -> object:
        """Return the effective value for a dotted key from a LoadedConfig."""
        section, _, field = key.partition(".")
        return getattr(getattr(loaded.settings, section), field)

    def _config_set(self, args: list[str]) -> None:
        if len(args) < 2:
            self._console.print("Usage: /config set <key> <value>")
            return
        key = args[0]
        raw_value = " ".join(args[1:])
        # Validate before touching disk — errors can never be saved.
        try:
            coerced = validate_override(key, raw_value)
        except ConfigError as exc:
            self._console.print(f"[red]Config error:[/red] {exc}")
            return
        # Capture old effective value before overwrite.
        old_value = self._resolve_setting_value(self._loaded, key)
        # Read → mutate → save (atomic).
        path = self._overrides_path()
        current, _ = load_overrides(path)
        current[key] = coerced
        save_overrides(path, current)
        # Reload (same path as /config reload) to pick up the new layer.
        try:
            self._loaded = self._reload_config()
        except ConfigError as exc:
            self._console.print(f"[red]Config reload failed:[/red] {exc}")
            return
        self._runtime.update_settings(self._loaded.settings)
        new_value = self._resolve_setting_value(self._loaded, key)
        self._console.print(f"{key}: {old_value!r} → {new_value!r}")
        if key in BOOT_ONLY_KEYS:
            note = "(saved — takes effect next session)"
            if key == "model.default":
                note += " — use /model use <name> to switch now"
            self._console.print(f"[dim]{note}[/dim]")
        # Audit.
        if self._runtime.audit is not None:
            self._runtime.audit.write(
                "config_change", setting=key, value=str(coerced), source="set"
            )
        self._print_config_warnings()

    def _config_unset(self, key: str) -> None:
        path = self._overrides_path()
        current, _ = load_overrides(path)
        if key not in current:
            self._console.print(f"[dim]no override set for {key}[/dim]")
            return
        del current[key]
        save_overrides(path, current)
        # Reload to pick up the reverted layer.
        try:
            self._loaded = self._reload_config()
        except ConfigError as exc:
            self._console.print(f"[red]Config reload failed:[/red] {exc}")
            return
        self._runtime.update_settings(self._loaded.settings)
        new_value = self._resolve_setting_value(self._loaded, key)
        source = self._loaded.sources.get(key, "default")
        self._console.print(f"{key}: {new_value!r} (from {source})")
        if self._runtime.audit is not None:
            self._runtime.audit.write("config_change", setting=key, value="unset", source="unset")
        self._print_config_warnings()

    def _config_reset_all(self) -> None:
        path = self._overrides_path()
        current, _ = load_overrides(path)
        if not self._confirm("Clear all config overrides?"):
            self._console.print("cancelled.")
            return
        count = len(current)
        save_overrides(path, {})
        try:
            self._loaded = self._reload_config()
        except ConfigError as exc:
            self._console.print(f"[red]Config reload failed:[/red] {exc}")
            return
        self._runtime.update_settings(self._loaded.settings)
        self._console.print(f"cleared {count} override(s).")
        if self._runtime.audit is not None:
            self._runtime.audit.write("config_change", setting="*", value="reset", source="reset")
        self._print_config_warnings()

    def _plan(self, args: list[str]) -> None:
        manager = self._runtime.plan_manager
        plan = manager.active
        if plan is None:
            self._console.print("[dim]No active plan.[/dim]")
            return
        if not args:
            self._console.print(plan_panel(plan, self._glyphs))
            self._console.print(f"[dim]Status: {plan.status}[/dim]")
            return
        if args[0] == "path":
            self._console.print(str(manager.artifact_path(plan)))
            return
        if args[0] == "cancel":
            if self._confirm(f"Cancel the active plan ({plan.task_id})?"):
                manager.cancel()
                self._console.print("[dim]Plan cancelled.[/dim]")
            return
        if args[0] == "revise":
            instruction = " ".join(args[1:]).strip()
            if not instruction:
                self._console.print("Usage: /plan revise <what should change>")
                return
            self._runtime.run_turn(f"Revise the active plan: {instruction}")
            self._console.print()
            return
        self._console.print("Usage: /plan | /plan path | /plan cancel | /plan revise <text>")

    def _cwd(self, args: list[str]) -> None:
        if not args:
            status = self._runtime.status()
            self._console.print(f"Workspace boundary: {status.workspace}")
            return
        if args[0] == "set" and len(args) > 1:
            new_workspace = Path(args[1]).expanduser().resolve()
            if not new_workspace.is_dir():
                self._console.print(f"[red]{new_workspace} is not a directory.[/red]")
                return
            if self._confirm(f"Change the workspace boundary to {new_workspace}?"):
                self._runtime.set_workspace(new_workspace)
                self._console.print(f"Workspace boundary: {new_workspace}")
            return
        self._console.print("Usage: /cwd | /cwd set <path>")

    def _doctor(self) -> None:
        from shellpilot.cli.doctor import run_doctor

        run_doctor(self._runtime.status().workspace)

    def _diff(self) -> None:
        diffs = self._runtime.recent_diffs
        if not diffs:
            self._console.print("[dim]No agent edits this session.[/dim]")
            return
        for diff in diffs[-5:]:
            self._console.print(render_diff(diff, self._glyphs))

    def _profile(self, args: list[str]) -> None:
        import dataclasses

        from shellpilot.config.model import VALID_PROFILES

        settings = self._runtime.settings
        if not args:
            self._console.print(f"Active profile: {settings.runtime.security_profile}")
            self._console.print(f"Available: {', '.join(VALID_PROFILES)} (trusted-local is v2)")
            return
        if args[0] == "use" and len(args) > 1:
            name = args[1]
            if name not in VALID_PROFILES:
                self._console.print(
                    f"[red]{name} is not a v1 profile.[/red] Available: {', '.join(VALID_PROFILES)}"
                )
                return
            new_runtime = dataclasses.replace(settings.runtime, security_profile=name)
            self._runtime.update_settings(dataclasses.replace(settings, runtime=new_runtime))
            if self._runtime.audit is not None:
                self._runtime.audit.write("config_change", setting="profile", value=name)
                self._runtime.audit.profile = name
            self._console.print(f"Switched to profile: {name}")
            return
        self._console.print("Usage: /profile | /profile use <supervised|balanced>")

    def _logs(self, args: list[str] | None = None) -> None:
        audit = self._runtime.audit
        if audit is None:
            self._console.print("[dim]Audit logging is not active in this session.[/dim]")
            return
        show_all = args is not None and args and args[0] == "all"
        session_filter = None if show_all else audit.session_id
        events = audit.tail(15, session_id=session_filter)
        if not events:
            label = "global audit" if show_all else "this session"
            self._console.print(f"[dim]No audit events for {label} yet.[/dim]")
            return
        for event in events:
            line = f"{event.get('timestamp', '?')} {event.get('event', '?')}"
            for key in ("tool", "command", "risk", "decision", "summary"):
                if event.get(key):
                    line += f" {key}={event[key]}"
            self._console.print(line, markup=False, highlight=False)
        self._console.print(f"[dim]Log file: {audit.path}[/dim]")

    def _export(self, args: list[str]) -> None:
        from shellpilot.persistence.json_store import atomic_write_text
        from shellpilot.persistence.paths import project_state_dir
        from shellpilot.persistence.sessions import SessionStore, session_markdown

        store = self._runtime.session
        if store is None or not store.path.is_file():
            self._console.print("[dim]No session transcript available to export.[/dim]")
            return
        workspace = self._runtime.status().workspace
        target = (
            Path(args[0]).expanduser()
            if args
            else project_state_dir(workspace) / "exports" / f"{store.session_id}.md"
        )
        atomic_write_text(target, session_markdown(SessionStore.load(store.path)))
        if self._runtime.audit is not None:
            self._runtime.audit.write("export", summary=str(target))
        self._console.print(f"Exported transcript to {target}")

    def _memory(self, args: list[str]) -> None:
        stores = self._runtime.memory
        if stores is None:
            self._console.print("[dim]Memory is not available this session.[/dim]")
            return
        action = args[0] if args else "show"
        if action == "show":
            stores.global_store.reload()
            stores.project_store.reload()
            block = stores.render(max_tokens=4000)
            if block:
                self._console.print(block, markup=False, highlight=False)
            else:
                self._console.print("[dim]No stored memory yet.[/dim]")
            self._console.print(f"[dim]Global: {stores.global_store.path}[/dim]")
            self._console.print(f"[dim]Project: {stores.project_store.path}[/dim]")
            return
        if action == "add":
            text = " ".join(args[1:]).strip()
            if not text:
                self._console.print("Usage: /memory add <preference text>")
                return
            if self._confirm(f'Add global preference "{text}"?'):
                preference = stores.global_store.add_preference(text, scope="global", source="user")
                self._audit_memory(f"add {preference.id}")
                self._console.print(f"Saved {preference.id}.")
            return
        if action == "forget":
            if len(args) < 2:
                self._console.print("Usage: /memory forget <id>")
                return
            entry_id = args[1]
            store = stores.find_store(entry_id)
            if store is None:
                self._console.print(f"[red]No memory entry {entry_id}.[/red] See /memory show.")
                return
            if self._confirm(f"Forget {entry_id}?"):
                store.remove(entry_id)
                self._audit_memory(f"forget {entry_id}")
                self._console.print(f"Removed {entry_id}.")
            return
        if action == "compact":
            self._memory_compact(stores)
            return
        self._console.print(
            "Usage: /memory show | /memory add <text> | /memory forget <id> | /memory compact"
        )

    def _audit_memory(self, summary: str) -> None:
        if self._runtime.audit is not None:
            self._runtime.audit.write("memory_update", summary=summary)

    def _memory_compact(self, stores: object) -> None:
        import json

        from shellpilot.llm.messages import Message
        from shellpilot.memory.store import MemoryStores, Preference
        from shellpilot.prompts.memory import MEMORY_COMPACT_PROMPT

        assert isinstance(stores, MemoryStores)
        by_store = {
            "global": stores.global_store,
            "project": stores.project_store,
        }
        all_preferences = {
            preference.id: (name, preference)
            for name, store in by_store.items()
            for preference in store.preferences
        }
        if len(all_preferences) < 2:
            self._console.print("[dim]Not enough preferences to compact.[/dim]")
            return
        entries = json.dumps(
            [
                {"id": p.id, "scope": p.scope, "text": p.text, "source": p.source}
                for _, p in all_preferences.values()
            ],
            indent=2,
        )
        try:
            reply = self._client.chat(
                self._runtime.model,
                [Message(role="user", content=MEMORY_COMPACT_PROMPT.format(entries=entries))],
                num_ctx=min(4096, self._runtime.budget.model_context_tokens),
            )
        except Exception as exc:  # noqa: BLE001 - optimization is best-effort
            self._console.print(f"[red]Memory compaction failed:[/red] {exc}")
            return
        content = reply.content.strip()
        start, end = content.find("["), content.rfind("]")
        if start == -1 or end <= start:
            self._console.print("[red]The model did not return a valid entry list.[/red]")
            return
        try:
            final_entries = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            self._console.print("[red]The model did not return valid JSON.[/red]")
            return
        final_ids = {str(e.get("id", "")) for e in final_entries if isinstance(e, dict)}
        unknown = final_ids - set(all_preferences)
        if unknown:
            self._console.print(f"[red]Optimization rejected:[/red] unknown ids {unknown}.")
            return
        dropped_user = [
            pid
            for pid, (_, p) in all_preferences.items()
            if p.source == "user" and pid not in final_ids
        ]
        if dropped_user:
            self._console.print(
                f"[red]Optimization rejected:[/red] it would drop user entries {dropped_user}."
            )
            return
        kept = len(final_ids)
        if not self._confirm(f"Apply optimization: {len(all_preferences)} preferences -> {kept}?"):
            return
        for name, store in by_store.items():
            new_preferences = []
            for entry in final_entries:
                if not isinstance(entry, dict):
                    continue
                pid = str(entry.get("id", ""))
                store_name, original = all_preferences[pid]
                if store_name != name:
                    continue
                text = str(entry.get("text", "")).strip() or original.text
                new_preferences.append(
                    Preference(
                        id=original.id,
                        scope=original.scope,
                        text=text,
                        source=original.source,
                        updated_at=original.updated_at,
                    )
                )
            store.replace_all(new_preferences, list(store.facts))
        self._audit_memory(f"compact {len(all_preferences)} -> {kept}")
        self._console.print(f"Memory compacted: {len(all_preferences)} -> {kept} preferences.")

    def _prefs(self, args: list[str]) -> None:
        stores = self._runtime.memory
        if stores is None:
            self._console.print("[dim]Memory is not available this session.[/dim]")
            return
        action = args[0] if args else "show"
        if action == "show":
            preferences = list(stores.global_store.preferences) + list(
                stores.project_store.preferences
            )
            if not preferences:
                self._console.print("[dim]No behavior preferences stored.[/dim]")
                return
            for preference in preferences:
                self._console.print(
                    f"[{preference.id}] ({preference.scope}, {preference.source}) "
                    f"{preference.text}",
                    markup=False,
                    highlight=False,
                )
            return
        if action == "edit":
            self._console.print(f"Global memory: {stores.global_store.path}")
            self._console.print(f"Project memory: {stores.project_store.path}")
            self._console.print("Edit by hand, then run /memory show to reload.")
            return
        self._console.print("Usage: /prefs show | /prefs edit")

    def _tools(self) -> None:
        profile = self._runtime.settings.runtime.security_profile
        table = Table(title=f"Tools (profile: {profile})")
        table.add_column("Tool")
        table.add_column("Side effect")
        table.add_column("Enabled")
        for spec in self._runtime.registry.specs():
            enabled = profile in spec.allowed_profiles
            table.add_row(
                spec.name,
                spec.side_effect.value,
                "[green]yes[/green]" if enabled else "[red]no[/red]",
            )
        self._console.print(table)

    def _skills(self) -> None:
        from shellpilot.skills.loader import is_enabled

        skills = self._runtime.skills
        if not skills:
            self._console.print("[dim]No skills discovered.[/dim]")
            return

        enabled = self._runtime.settings.skills.enabled
        snapshot = self._runtime.context_snapshot()
        decision_by_key = {
            (decision.skill, decision.root): decision for decision in snapshot.decisions
        }

        table = Table(title="Skills")
        table.add_column("Skill")
        table.add_column("Root")
        table.add_column("Status")
        table.add_column("Active")
        table.add_column("Details", overflow="fold")

        for index, skill in enumerate(skills):
            decision = None
            if index < len(snapshot.decisions):
                candidate = snapshot.decisions[index]
                if candidate.skill == skill.name and candidate.root == skill.root:
                    decision = candidate
            if decision is None:
                decision = decision_by_key.get((skill.name, skill.root))

            if not skill.valid:
                status_cell = f"[red]invalid: {skill.error}[/red]"
            elif skill.root == "builtin" and skill.name == "planning":
                status_cell = "builtin"
            elif is_enabled(skill, enabled):
                status_cell = "enabled"
            else:
                status_cell = "[dim]disabled[/dim]"

            active = bool(decision.injected) if decision is not None else False
            active_cell = "[green]yes[/green]" if active else "[dim]no[/dim]"
            details: list[str] = []
            if decision is not None:
                if decision.matched_triggers:
                    triggers = ", ".join(trigger.value for trigger in decision.matched_triggers)
                    details.append(f"triggers: {triggers}")
                if decision.reason:
                    details.append(decision.reason)
                if decision.resource_summary:
                    details.append(decision.resource_summary)
                if decision.script_summary:
                    details.append(decision.script_summary)
            details.extend(f"[dim]{warning}[/dim]" for warning in skill.warnings)
            details_cell = "\n".join(details) if details else "[dim]-[/dim]"
            table.add_row(skill.name, skill.root, status_cell, active_cell, details_cell)

        self._console.print(table)

    def _context(self) -> None:
        snapshot = self._runtime.context_snapshot()
        budget = self._runtime.status().budget
        table = Table(title="Context breakdown")
        table.add_column("Block")
        table.add_column("Source")
        table.add_column("Tokens", justify="right")
        table.add_column("Injected")
        for block in snapshot.blocks:
            injected = "[green]yes[/green]" if block.injected else "[dim]no[/dim]"
            table.add_row(block.name, block.source, str(block.est_tokens), injected)
        tool_tokens = self._runtime.tool_schema_tokens()
        table.add_row(
            "tool schemas",
            "tools",
            str(tool_tokens),
            "[green]yes[/green]",
        )
        history_tokens, history_messages = self._runtime.history_token_estimate()
        table.add_row(
            "history",
            f"{history_messages} messages",
            str(history_tokens),
            "[green]yes[/green]",
        )
        total = snapshot.est_system_tokens + tool_tokens + history_tokens
        table.add_row(
            "TOTAL",
            f"of {budget.model_context_tokens} (compact at {budget.compact_at_tokens})",
            str(total),
            "",
        )
        self._console.print(table)

    def _attach(self, args: list[str]) -> None:
        """Stage an image for the next user message, or list staged images."""
        queue = self._attachments
        if queue is None:
            self._console.print("[dim]Attachment staging is not available this session.[/dim]")
            return

        if not args:
            # Bare /attach: list currently staged files
            if not queue.paths:
                self._console.print("[dim]No attachments staged.[/dim]")
            else:
                for p in queue.paths:
                    self._console.print(f"  {p.name}")
            return

        # /attach <path>
        raw = " ".join(args)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (self._runtime.status().workspace / raw).resolve()

        # Validate the file eagerly (extension, existence, size)
        try:
            ref = load_image(candidate)
        except AttachmentError as exc:
            self._console.print(f"[red]Cannot attach:[/red] {exc}")
            return

        # Check vision capability before staging
        if "vision" not in self._client.model_capabilities(self._runtime.model):
            self._console.print(
                f"[yellow]{self._runtime.model}[/yellow] does not support vision. "
                "Use /model use to switch to a vision-capable model, then try again."
            )
            return

        queue.stage(candidate)
        # Use the already-loaded ref so a file vanishing between calls can't crash.
        size = len(base64.b64decode(ref.data_b64))
        human_size = (
            f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"
        )
        self._console.print(
            f"Attached [bold]{candidate.name}[/bold] ({human_size})"
            " — it will be sent with your next message."
        )

    def _compact(self, args: list[str]) -> None:
        import dataclasses

        if args and args[0] == "status":
            status = self._runtime.status()
            budget = status.budget
            auto = "on" if self._runtime.settings.runtime.auto_compact else "off"
            self._console.print(f"Model: {status.model}")
            self._console.print(f"Detected context: {budget.model_context_tokens} tokens")
            self._console.print(f"Current prompt estimate: {status.estimated_prompt_tokens} tokens")
            self._console.print(f"Compact at: {budget.compact_at_tokens} tokens")
            self._console.print(f"Hard limit: {budget.hard_limit_tokens} tokens")
            self._console.print(f"Automatic compaction: {auto}")
            plan = self._runtime.plan_manager.active
            self._console.print(f"Active plan: {'yes' if plan is not None else 'no'}")
            return
        if args and args[0] == "auto":
            if len(args) > 1 and args[1] in ("on", "off"):
                settings = self._runtime.settings
                new_runtime = dataclasses.replace(settings.runtime, auto_compact=args[1] == "on")
                self._runtime.update_settings(dataclasses.replace(settings, runtime=new_runtime))
                if self._runtime.audit is not None:
                    self._runtime.audit.write(
                        "config_change", setting="auto_compact", value=args[1]
                    )
                self._console.print(f"Automatic compaction: {args[1]}")
                return
            self._console.print("Usage: /compact auto on | /compact auto off")
            return
        adjusted = self._runtime.compact_now()
        if adjusted:
            self._console.print(f"[dim]Compacted: adjusted {adjusted} messages.[/dim]")
        else:
            self._console.print("[dim]Nothing to compact.[/dim]")
