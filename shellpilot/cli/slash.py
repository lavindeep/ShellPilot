"""Slash commands: user controls for the harness itself (design section 20.1)."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.table import Table
from rich.text import Text

from shellpilot.cli.attachments import AttachmentError, AttachmentQueue, load_image
from shellpilot.cli.render import plan_panel, render_diff
from shellpilot.cli.theme import UNICODE_GLYPHS, Glyphs
from shellpilot.config.loader import (
    BOOT_ONLY_KEYS,
    HIGH_STAKES_KEYS,
    ConfigError,
    LoadedConfig,
    validate_override,
)
from shellpilot.config.model import (
    TESTED_FAMILIES,
    is_cloud_model,
    is_egressing,
    is_tested_model,
)
from shellpilot.config.overrides import load_overrides, overrides_path, save_overrides
from shellpilot.llm.client import LLMClient
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.skills.model import SkillTrigger


class SlashAction(Enum):
    CONTINUE = "continue"
    CLEAR = "clear"
    EXIT = "exit"
    MANUAL_SHELL = "manual_shell"


HELP_ROWS: list[tuple[str, str]] = [
    ("/help", "Show available commands."),
    ("/exit", "Exit ShellPilot."),
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
    ("/memory show", "Show preferences (scope/source) and facts with ids, plus file paths."),
    ("/memory add <text>", "Add a global behavior preference after confirmation."),
    ("/memory forget <id>", "Remove a memory entry after confirmation."),
    ("/memory compact", "Model-assisted preference cleanup, approved before saving."),
    ("/shell", "Enter Manual Shell mode (raw shell, user-typed)."),
    ("/attach <path>", "Stage an image to send with your next message (vision models only)."),
    ("/attach", "List currently staged images."),
    ("/skills", "List discovered skills with triggers, resources, reasons, and active status."),
]


def command_words() -> list[str]:
    """Completion phrases derived from HELP_ROWS: drop <args> placeholders."""
    words: list[str] = []
    for entry, _ in HELP_ROWS:
        phrase = " ".join(part for part in entry.split() if not part.startswith("<"))
        if phrase and phrase not in words:
            words.append(phrase)
    return words


@dataclasses.dataclass(frozen=True)
class SlashMenuItem:
    """One row of the in-app slash menu (design section 31.20).

    ``fill`` is the command text Tab/Enter inserts (the `<arg>` placeholders
    dropped); ``label`` is the displayed form keeping the placeholders so the user
    sees what a command takes; ``takes_args`` drives smart-Enter — an argless
    command runs on Enter, an arg command fills ``fill + " "`` and waits.
    """

    fill: str
    label: str
    description: str
    takes_args: bool


def slash_menu_items() -> list[SlashMenuItem]:
    """The full menu, one row per HELP_ROWS entry (no dedupe — arg variants like
    ``/model use`` and ``/model list`` are distinct rows)."""
    items: list[SlashMenuItem] = []
    for entry, purpose in HELP_ROWS:
        parts = entry.split()
        fill = " ".join(part for part in parts if not part.startswith("<"))
        takes_args = any(part.startswith("<") for part in parts)
        items.append(
            SlashMenuItem(fill=fill, label=entry, description=purpose, takes_args=takes_args)
        )
    return items


def slash_menu_matches(text: str, items: Sequence[SlashMenuItem]) -> list[SlashMenuItem]:
    """Items whose ``fill`` starts with the typed text (case-insensitive).

    Text that does not begin with ``/`` (or is empty) matches nothing — the menu
    is closed. A bare ``/`` matches everything.
    """
    needle = text.strip().lower()
    if not needle.startswith("/"):
        return []
    return [it for it in items if it.fill.lower().startswith(needle)]


def slash_menu_open(text: str) -> bool:
    """True while the user is still typing a command token: the text begins with
    ``/`` and contains no whitespace yet. The first space (or newline) ends the
    token — the user has filled a command or moved into its args — so the menu
    closes. Approval/busy gating is the caller's (it owns that state)."""
    return text.startswith("/") and not any(char.isspace() for char in text)


def slash_menu_window(index: int, total: int, visible: int = 3) -> int:
    """First visible row so the selected ``index`` stays on screen as a fixed
    ``visible``-row window scrolls through a longer list (selected kept off the
    very top until the list nears its end; clamped at both ends)."""
    if total <= visible:
        return 0
    return max(0, min(index - 1, total - visible))


def needs_terminal(line: str) -> bool:
    """True when a slash line must run with the real terminal (run_in_terminal):
    it confirms, prompts for cloud consent, prints to its own stdout, or preloads.

    The full-screen app (§31.17) runs fast, display-only commands on the loop
    thread with a pane-capturing console; the forms below instead call
    ``self._confirm`` / the cloud-consent prompt, print to their own
    ``Console()`` (``/doctor`` → ``run_doctor``), or do slow preload work
    (``/model use``), none of which can run on the event-loop thread.

    NOTE: this enumerates every confirm()/consent/own-stdout/preload command. If
    a new one is added (a new ``self._confirm`` call site, a cloud-consent path,
    a handler that builds its own console, or a slow preload), add it here too.
    """
    parts = line.strip().split()
    if not parts:
        return False
    cmd = parts[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if cmd == "/shell":  # manual shell
        return True
    if cmd == "/clear":  # confirm
        return True
    if cmd == "/doctor":  # run_doctor prints to its own Console()/stdout
        return True
    if cmd == "/plan" and sub == "cancel":  # confirm
        return True
    if cmd == "/cwd" and sub == "set":  # confirm
        return True
    if cmd == "/config" and sub in ("set", "reset"):  # confirm
        return True
    if cmd == "/memory" and sub in ("add", "forget", "compact"):  # confirm
        return True
    if cmd == "/model" and sub == "use":  # cloud-consent prompt + slow preload
        return True
    return False


def needs_worker(line: str) -> bool:
    """True for a slash command that runs a model turn, so it must execute on the
    worker thread — NOT the loop thread (would freeze the UI and the approval-gate
    Future could only be resolved by the now-blocked loop) and NOT under
    ``run_in_terminal`` (which suspends the app while the turn marshals to it).

    Currently only ``/plan revise <text>`` (it calls ``runtime.run_turn``). A bare
    ``/plan revise`` with no text only prints usage, so it stays on the loop path.

    NOTE: if another slash command starts driving ``run_turn``, add it here.
    """
    parts = line.strip().split()
    return len(parts) >= 3 and parts[0].lower() == "/plan" and parts[1].lower() == "revise"


def needs_background(line: str) -> bool:
    """True for a NON-interactive slash command that makes a blocking network/IO
    call. It must run off the loop thread (the event loop must never block — a
    hung Ollama would otherwise freeze the TUI for the client timeout with no
    Ctrl-C), but it needs no real terminal (no confirm/consent/own-stdout), so the
    router runs it on the worker and marshals the captured output into the pane.

    ``/model list`` (``GET /api/tags``) and ``/attach <path>`` (``POST /api/show``
    for the vision-capability check + image load). A bare ``/attach`` only lists
    already-staged images in memory, so it stays on the loop path.

    NOTE: add any other non-interactive command that makes a blocking network/IO
    call here — the criterion is "blocks the loop", not just confirm/consent.
    """
    parts = line.strip().split()
    if not parts:
        return False
    cmd = parts[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if cmd == "/model" and sub == "list":
        return True
    if cmd == "/attach" and len(parts) > 1:  # /attach <path> probes /api/show
        return True
    return False


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


# Per-key risk phrasing for the HIGH_STAKES_KEYS confirm-gate in /config set.
_HIGH_STAKES_RISK: dict[str, str] = {
    "model.allow_cloud": "enables cloud egress — model calls may leave the device",
    "tools.web": "enables web egress — searches and fetches leave the device",
    "model.base_url": "changes the model endpoint — requests go to a different host",
    "runtime.security_profile": "lowers the local safety profile — low-risk commands may auto-run",
}

# Starter config written by `/config edit` ONLY when no config.toml exists yet.
# Every key is commented out, so the file is valid TOML that load_config accepts
# and that resolves to the built-in defaults — i.e. an effectively empty config.
# It is never written over an existing file (config.toml is user-owned; the
# program never rewrites it). Keep it concise: common keys plus the egress/safety
# keys, with honest notes on which are config-file-only vs high-stakes.
_STARTER_CONFIG = """\
# ShellPilot config — starter template (every key is commented out, so it
# resolves to the built-in defaults). Uncomment and edit the keys you want.
# ShellPilot never rewrites this file; it is yours to edit by hand.
#
# Boot-only keys (model.*, ui.*, instructions.*, tools.web) take effect next
# session. Runtime-settable keys can also be changed live with /config set.
# Egress/safety keys (tools.web, model.base_url, model.allow_cloud,
# runtime.security_profile) are settable here OR via a confirm-gated /config set,
# but NEVER via an environment variable. The structural keys model.options and
# skills.enabled are config-file-only: edit them here, never via /config set.

[model]
# default = "gemma4:e4b"
# keep_alive = "5m"               # how long Ollama keeps the model warm
# base_url = "http://localhost:11434"  # high-stakes: changes the endpoint
# allow_cloud = false             # high-stakes: master cloud-egress switch

# [model.options]                 # config-file-only: verbatim Ollama options
# repeat_penalty = 1.3            # num_ctx is reserved and ignored here

[runtime]
# security_profile = "balanced"   # high-stakes: "balanced" | "supervised"
# auto_compact = true

[tools]
# web = false                     # high-stakes: registers web_search + web_fetch

[skills]
# enabled = ["my-skill"]          # config-file-only: skill folders to activate

[privacy]
# allow_sensitive_reads = "ask"   # ask | never | always

[ui]
# theme = "default"
# glyphs = "auto"                 # auto | unicode | ascii
"""


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
        tty: bool = True,
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
        self._tty = tty

    def handle(self, line: str) -> SlashAction:
        parts = line.strip().split()
        command, args = parts[0].lower(), parts[1:]

        if command == "/exit":
            return SlashAction.EXIT
        if command == "/shell":
            return SlashAction.MANUAL_SHELL
        if command == "/help":
            self._help()
        elif command == "/clear":
            if self._clear():
                return SlashAction.CLEAR
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

    def _clear(self) -> bool:
        if self._confirm("Clear the conversation (also cancels the active plan)?"):
            had_plan = self._runtime.plan_manager.active is not None
            self._runtime.clear_history()
            if had_plan:
                self._console.print("[dim]Conversation cleared and active plan cancelled.[/dim]")
            else:
                self._console.print("[dim]Conversation cleared.[/dim]")
            return True
        return False

    def _status(self) -> None:
        status = self._runtime.status()
        self._console.print(f"Model: {status.model}")
        self._console.print(f"Profile: {status.profile}")
        self._console.print(self._locality_line(status.model))
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

    def _locality_line(self, model: str) -> Text:
        """Honest one-line locality readout derived from the egress signal.

        REMOTE in amber names the off-box host (the configured non-loopback
        endpoint, or 'cloud' for a -cloud model proxied through loopback);
        local in dim otherwise (design section 15.2).
        """
        from shellpilot.llm.ollama import is_loopback_url

        base_url = self._loaded.settings.model.base_url
        if not is_egressing(model, base_url):
            return Text("Locality: local", style="sp.dim")
        if not is_loopback_url(base_url):
            host = (urlsplit(base_url).hostname or "").rstrip(".") or base_url
        else:
            host = "cloud" if is_cloud_model(model) else base_url
        return Text(f"Locality: REMOTE — {host}", style="sp.warn")

    def _model(self, args: list[str]) -> None:
        if not args:
            status = self._runtime.status()
            self._console.print(
                f"Active model: {status.model} "
                f"(context {status.budget.model_context_tokens} tokens)"
            )
            return
        if args[0] == "list":
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
            from shellpilot.persistence.workspace_state import save_last_model

            name = args[1]
            installed = {model.name for model in self._client.list_models()}
            # Cloud models are absent from /api/tags — skip the availability gate
            # for them (the typo-catch survives for local names).
            if name not in installed and not is_cloud_model(name):
                self._console.print(f"[red]{name} is not installed.[/red] See /model list.")
                return
            # Cloud-egress consent boundary (design section 15.2): switching to a
            # cloud/remote model mid-session requires allow_cloud + per-session
            # consent BEFORE set_model/_preload touch it. On reject: no switch,
            # no preload, no egress.
            from shellpilot.cli.terminal import _resolve_cloud_consent

            base_url = self._loaded.settings.model.base_url
            egressing = is_egressing(name, base_url)
            if not _resolve_cloud_consent(
                self._console, self._loaded.settings, name, tty=self._tty
            ):
                return
            self._runtime.set_model(name)
            workspace = self._runtime.status().workspace
            try:
                save_last_model(workspace, name)
            except OSError as exc:
                self._console.print(f"[dim]Warning: could not save model choice: {exc}[/dim]")
            if self._preload is not None:
                self._preload(name)
            if egressing and self._runtime.audit is not None:
                self._runtime.audit.write(
                    "cloud_consent_granted",
                    model=name,
                    host=(urlsplit(base_url).hostname or "").rstrip("."),
                )
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
            self._config_edit()
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
        elif action == "unset" and len(args) > 1:
            self._config_unset(args[1])
        elif action == "reset" and len(args) == 1:
            self._config_reset_all()
        else:
            self._console.print(
                "Usage: /config show | /config edit | /config reload"
                " | /config set <key> <value>"
                " | /config unset <key>"
                " | /config reset"
            )

    def _config_edit(self) -> None:
        path = self._user_config_file
        created = False
        if not path.exists():
            # No user config yet: write a commented starter so the printed path
            # is real and editable. NEVER touch an existing config.toml — it is
            # user-owned and the program never rewrites it.
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_STARTER_CONFIG)
            created = True
        self._console.print(f"User config: {path}")
        if created:
            self._console.print(
                "[dim]Created a starter config (every key commented out, so it "
                "resolves to defaults). Uncomment and edit the keys you want.[/dim]"
            )
        self._console.print("Edit the file, then run /config reload.")
        self._console.print(
            "[dim]Boot-only keys take effect next session; runtime-settable keys "
            "can also use /config set <key> <value>.[/dim]"
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
        # Egress / safety keys: amber warning + explicit confirm before persisting.
        if key in HIGH_STAKES_KEYS:
            risk = _HIGH_STAKES_RISK[key]
            self._console.print(Text(f"⚠ {key} {risk}.", style="sp.warn"))
            # security_profile is read per turn (conversation.py), so a set
            # applies this session; the egress keys are boot-only. Never defer a
            # live safety downgrade to "next session".
            when = "next session" if key in BOOT_ONLY_KEYS else "this session (next turn)"
            self._console.print(
                Text(
                    f"It takes effect {when} and persists in overrides.json "
                    f"until /config unset {key}.",
                    style="sp.warn",
                )
            )
            if key == "runtime.security_profile":
                self._console.print(
                    Text(
                        "Use /profile use <profile> for an unsaved, session-only change.",
                        style="sp.warn",
                    )
                )
            if not self._confirm(f"Persist {key} = {coerced!r} to overrides?"):
                self._console.print("[dim]unchanged.[/dim]")
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
        # High-stakes keys already printed their own when/persist note above.
        if key in BOOT_ONLY_KEYS and key not in HIGH_STAKES_KEYS:
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

    def _logs(self, args: list[str]) -> None:
        audit = self._runtime.audit
        if audit is None:
            self._console.print("[dim]Audit logging is not active in this session.[/dim]")
            return
        show_all = args[:1] == ["all"]
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
            block = stores.render(max_tokens=4000, meta=True)
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
        snapshot = self._runtime.context_snapshot()
        if not snapshot.decisions:
            self._console.print("[dim]No skills discovered.[/dim]")
            return

        table = Table(title="Skills")
        table.add_column("Skill")
        table.add_column("Root")
        table.add_column("Triggers")
        table.add_column("Status")
        table.add_column("Active")
        table.add_column("Resources", overflow="fold")
        table.add_column("Reason", overflow="fold")

        for decision in snapshot.decisions:
            triggers_cell = (
                ", ".join(trigger.value for trigger in decision.triggers)
                if decision.triggers
                else "[dim]-[/dim]"
            )
            if decision.reason.startswith("invalid:"):
                status_cell = f"[red]{decision.reason}[/red]"
            elif decision.reason == "disabled":
                status_cell = "[dim]disabled[/dim]"
            elif decision.root == "builtin" and SkillTrigger.ENABLED not in decision.triggers:
                status_cell = "builtin"
            else:
                status_cell = "enabled"

            active = decision.injected
            active_cell = "[green]yes[/green]" if active else "[dim]no[/dim]"
            resources = [decision.resource_summary, decision.script_summary]
            resources_cell = "\n".join(part for part in resources if part) or "[dim]-[/dim]"
            reasons = [decision.reason] if decision.reason else []
            reasons.extend(f"[dim]{warning}[/dim]" for warning in decision.warnings)
            reason_cell = "\n".join(reasons) if reasons else "[dim]-[/dim]"
            table.add_row(
                decision.skill,
                decision.root,
                triggers_cell,
                status_cell,
                active_cell,
                resources_cell,
                reason_cell,
            )

        self._console.print(table)

    def _context(self) -> None:
        snapshot = self._runtime.context_snapshot()
        budget = self._runtime.status().budget
        table = Table(title="Context breakdown")
        table.add_column("Block")
        table.add_column("Source")
        table.add_column("Tokens", justify="right")
        table.add_column("Injected")
        table.add_column("Reason", overflow="fold")
        for block in snapshot.blocks:
            injected = "[green]yes[/green]" if block.injected else "[dim]no[/dim]"
            table.add_row(block.name, block.source, str(block.est_tokens), injected, block.reason)
        tool_tokens = self._runtime.tool_schema_tokens()
        table.add_row(
            "tool schemas",
            "tools",
            str(tool_tokens),
            "[green]yes[/green]",
            "",
        )
        history_tokens, history_messages = self._runtime.history_token_estimate()
        table.add_row(
            "history",
            f"{history_messages} messages",
            str(history_tokens),
            "[green]yes[/green]",
            "",
        )
        total = snapshot.est_system_tokens + tool_tokens + history_tokens
        table.add_row(
            "TOTAL",
            f"of {budget.model_context_tokens} (compact at {budget.compact_at_tokens})",
            str(total),
            "",
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
        size = ref.size_bytes
        human_size = (
            f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"
        )
        self._console.print(
            f"Attached [bold]{candidate.name}[/bold] ({human_size})"
            " — it will be sent with your next message."
        )

    def _compact(self, args: list[str]) -> None:
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
