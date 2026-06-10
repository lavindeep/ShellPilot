"""Interactive terminal session: rich rendering plus the REPL loop."""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from shellpilot import __version__
from shellpilot.cli.slash import SlashAction, SlashDispatcher
from shellpilot.config.loader import ConfigError, LoadedConfig, load_config
from shellpilot.llm.ollama import OllamaClient, OllamaError
from shellpilot.memory.agents_md import BehaviorInstructions, load_behavior_instructions
from shellpilot.persistence.paths import AppPaths, project_state_dir
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime

PROMPT = "[bold cyan]\\[AI] >[/bold cyan] "


class TerminalUI:
    """RuntimeUI implementation over a rich console."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def stream_token(self, token: str) -> None:
        self._console.print(token, end="", markup=False, highlight=False, soft_wrap=True)

    def show_status(self, text: str) -> None:
        self._console.print(f"[dim]{escape(text)}[/dim]")

    def show_error(self, text: str) -> None:
        self._console.print(f"[red]{escape(text)}[/red]")

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        rendered = " ".join(f"{key}={value!r}" for key, value in arguments.items())
        self._console.print(f"[dim]→ {escape(name)} {escape(rendered)}[/dim]")

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        mark = "[green]✓[/green]" if success else "[red]✗[/red]"
        self._console.print(f"[dim]{mark} {escape(summary)}[/dim]")

    def show_command_output(self, line: str) -> None:
        self._console.print(escape(line), markup=False, highlight=False)

    def ask_approval(self, request: ApprovalRequest) -> bool:
        """Approval UX per design section 20: high risk requires typing 'run'."""
        risk_color = "red" if request.risk is RiskLevel.HIGH else "yellow"
        self._console.print(
            f"\n[{risk_color} bold]{request.risk.value.capitalize()} risk "
            f"{request.kind}[/{risk_color} bold]"
        )
        self._console.print(f"CWD: {request.cwd}")
        self._console.print(f"Command: {escape(request.display)}")
        if request.purpose:
            self._console.print(f"Purpose: {escape(request.purpose)}")
        elif request.reasons:
            self._console.print(f"Why flagged: {escape('; '.join(request.reasons))}")
        try:
            if request.risk is RiskLevel.HIGH:
                answer = self._console.input(
                    'Run it? Type "run" to execute, or press Enter to cancel: '
                )
                return answer.strip().lower() == "run"
            answer = self._console.input("Approve? \\[y/N] ")
            return answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def ask_plan_approval(self, rendered: str, path: str) -> tuple[str, str]:
        self._console.print()
        self._console.print(escape(rendered), markup=False, highlight=False)
        self._console.print(f"[dim]Plan saved to {escape(path)}[/dim]")
        try:
            while True:
                answer = (
                    self._console.input("Approve plan? \\[y]es / \\[e]dit / \\[n]o: ")
                    .strip()
                    .lower()
                )
                if answer in ("y", "yes"):
                    return "y", ""
                if answer in ("e", "edit"):
                    revision = self._console.input("Describe the changes you want: ").strip()
                    return "e", revision
                if answer in ("n", "no", ""):
                    return "n", ""
        except (EOFError, KeyboardInterrupt):
            return "n", ""


def config_files(workspace: Path, env: dict[str, str], paths: AppPaths) -> tuple[Path, Path]:
    """User and project config paths; SHELLPILOT_CONFIG overrides the user path."""
    user_file = (
        Path(env["SHELLPILOT_CONFIG"]) if env.get("SHELLPILOT_CONFIG") else (paths.user_config_file)
    )
    return user_file, project_state_dir(workspace) / "config.toml"


def run_interactive(workspace: Path) -> int:
    console = Console()
    env = dict(os.environ)
    paths = AppPaths.default()
    user_file, project_file = config_files(workspace, env, paths)

    def load() -> LoadedConfig:
        return load_config(user_config_file=user_file, project_config_file=project_file, env=env)

    try:
        loaded = load()
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {escape(str(exc))}")
        return 2
    settings = loaded.settings

    client = OllamaClient(
        base_url=settings.model.base_url,
        reasoning=settings.model.reasoning,
    )
    if not client.health():
        console.print(
            "[red]Cannot reach Ollama at "
            f"{settings.model.base_url}.[/red] Is `ollama serve` running? "
            "Run `shellpilot doctor` for a full environment check."
        )
        return 1
    installed = {model.name for model in client.list_models()}
    if settings.model.default not in installed:
        console.print(
            f"[red]Model {settings.model.default} is not installed.[/red] "
            f"Try: ollama pull {settings.model.default}"
        )
        return 1

    if settings.instructions.load_agents_md:
        detected = client.model_context_length(settings.model.default)
        cap = min(1500, (detected or 8192) // 10)
        behavior = load_behavior_instructions(paths.config_dir, workspace, max_tokens=cap)
    else:
        behavior = BehaviorInstructions(global_text=None, project_text=None)

    ui = TerminalUI(console)
    runtime = ConversationRuntime(
        llm=client, settings=settings, workspace=workspace, behavior=behavior, ui=ui
    )
    dispatcher = SlashDispatcher(
        runtime=runtime,
        client=client,
        console=console,
        loaded=loaded,
        user_config_file=user_file,
        reload_config=load,
    )

    console.print(
        f"[bold]ShellPilot {__version__}[/bold] — model {runtime.model}, "
        f"profile {settings.runtime.security_profile}"
    )
    console.print(f"[dim]Workspace: {workspace} — /help for commands, /exit to quit.[/dim]")

    while True:
        try:
            line = console.input(PROMPT).strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("[dim](Ctrl-C — use /exit to quit)[/dim]")
            continue
        if not line:
            continue
        if line.startswith("/"):
            if dispatcher.handle(line) is SlashAction.EXIT:
                break
            continue
        try:
            runtime.run_turn(line)
            console.print()
        except KeyboardInterrupt:
            console.print()
            ui.show_status("Interrupted.")
        except OllamaError as exc:
            console.print()
            ui.show_error(f"Model call failed: {exc}")
    return 0
