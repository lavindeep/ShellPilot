"""Interactive terminal session: rich rendering plus the REPL loop."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.padding import Padding

from shellpilot import __version__
from shellpilot.cli.input import PromptContext, make_input
from shellpilot.cli.manual_shell import manual_shell_loop
from shellpilot.cli.render import (
    approval_cwd,
    approval_info,
    banner,
    plan_panel,
    plan_step_line,
    render_diff,
)
from shellpilot.cli.render import (
    tool_call as render_tool_call,
)
from shellpilot.cli.render import (
    tool_result as render_tool_result,
)
from shellpilot.cli.render import (
    turn_stats as render_turn_stats,
)
from shellpilot.cli.slash import SlashAction, SlashDispatcher, command_words
from shellpilot.cli.streaming import AviationSpinner, ResponseStream
from shellpilot.cli.theme import UNICODE_GLYPHS, Glyphs, build_console, resolve_glyphs
from shellpilot.config.loader import ConfigError, LoadedConfig, load_config
from shellpilot.config.model import Settings
from shellpilot.llm.ollama import OllamaClient, OllamaError
from shellpilot.memory.agents_md import BehaviorInstructions, load_behavior_instructions
from shellpilot.memory.store import MemoryFormatError, MemoryStore, MemoryStores, project_id_for
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.persistence.paths import AppPaths, project_state_dir
from shellpilot.persistence.sessions import SessionStore
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.events import TurnStats
from shellpilot.runtime.planner import TaskPlan


class TerminalUI:
    """RuntimeUI implementation over a rich console."""

    def __init__(
        self,
        console: Console,
        *,
        glyphs: Glyphs = UNICODE_GLYPHS,
        spinner: bool = True,
    ) -> None:
        self._console = console
        self._glyphs = glyphs
        self._stream = ResponseStream(console)
        self._spinner = AviationSpinner(console, glyphs, enabled=spinner)

    def begin_response(self) -> None:
        self._spinner.start()

    def end_response(self) -> None:
        self._spinner.stop()
        self._stream.finish()

    def turn_finished(self, stats: TurnStats) -> None:
        self._console.print(
            render_turn_stats(
                stats.elapsed_s, stats.context_tokens, stats.context_pct, warn=stats.warn
            )
        )

    def stream_token(self, token: str) -> None:
        self._spinner.stop()
        self._stream.feed(token)

    def show_status(self, text: str) -> None:
        self._console.print(f"[sp.dim]{escape(text)}[/sp.dim]")

    def show_error(self, text: str) -> None:
        self._console.print(f"[sp.error]{escape(text)}[/sp.error]")

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        summary = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        if len(summary) > 80:
            summary = summary[:79] + self._glyphs.ellipsis
        self._console.print(render_tool_call(name, summary, self._glyphs))

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._console.print(render_tool_result(success, summary, self._glyphs))

    def show_command_output(self, line: str) -> None:
        self._console.print("    " + line, style="sp.dim", markup=False, highlight=False)

    def show_plan_progress(self, plan: TaskPlan) -> None:
        for index, step in enumerate(plan.steps, 1):
            self._console.print(Padding(plan_step_line(index, step, self._glyphs), (0, 0, 0, 2)))

    def _plain_badges(self) -> bool:
        return self._console.no_color or not self._console.is_terminal

    def ask_approval(self, request: ApprovalRequest) -> bool:
        """Badge-block approval (section 31.5); high risk requires typing 'run'.

        No head line here: the tool-call line printed just before the approval
        already names the action, so repeating it would duplicate output.
        """
        self._console.print()
        if request.diff:
            self._console.print(Padding(render_diff(request.diff, self._glyphs), (0, 0, 0, 2)))
        self._console.print(approval_info(request, plain_badge=self._plain_badges()))
        self._console.print(approval_cwd(request))
        try:
            if request.risk is RiskLevel.HIGH:
                answer = self._console.input(
                    '  Type [sp.risk.high]"run"[/sp.risk.high] to execute, '
                    "or press Enter to cancel: "
                )
                return answer.strip().lower() == "run"
            answer = self._console.input("  Approve? [sp.dim]\\[y/n][/sp.dim] ")
            return answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        self._console.print()
        self._console.print(plan_panel(plan, self._glyphs))
        self._console.print(f"[sp.faint]{escape(path)}[/sp.faint]")
        try:
            while True:
                answer = (
                    self._console.input(
                        "Approve plan? [sp.dim]\\[y]es / \\[e]dit / \\[n]o[/sp.dim] "
                    )
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


def run_interactive(workspace: Path, resume: str | None = None) -> int:
    console = build_console(Settings())
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
    if settings.ui.no_color:
        console = build_console(settings)
    glyphs = resolve_glyphs(settings.ui.glyphs, console)

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

    audit = AuditLogger(
        path=paths.state_dir / "audit.jsonl",
        session_id=uuid.uuid4().hex[:12],
        workspace=workspace,
        profile=settings.runtime.security_profile,
        redact=settings.privacy.redact_secrets,
    )
    audit.write("session_start", model=settings.model.default)

    sessions_dir = SessionStore.sessions_dir(workspace)
    restored = None
    if resume is not None:
        session_path = (
            SessionStore.latest(sessions_dir)
            if resume == "latest"
            else SessionStore.find(sessions_dir, resume)
        )
        if session_path is None:
            console.print(
                f"[red]No saved session to resume[/red] "
                f"({'none found' if resume == 'latest' else resume}) in {sessions_dir}."
            )
            return 1
        restored = SessionStore.load(session_path)
    session = SessionStore(
        sessions_dir,
        restored.session_id if restored is not None else SessionStore.new_session_id(),
        redact=settings.privacy.redact_secrets,
    )
    session.write_meta(
        model=settings.model.default,
        profile=settings.runtime.security_profile,
        workspace=workspace,
    )

    try:
        memory = MemoryStores(
            global_store=MemoryStore(
                paths.config_dir / "memory.json", redact=settings.privacy.redact_secrets
            ),
            project_store=MemoryStore(
                project_state_dir(workspace) / "memory.json",
                project_id=project_id_for(workspace),
                redact=settings.privacy.redact_secrets,
            ),
        )
    except MemoryFormatError as exc:
        console.print(f"[sp.error]Memory file problem:[/sp.error] {escape(str(exc))}")
        console.print("[sp.dim]Continuing without stored memory this session.[/sp.dim]")
        memory = None

    ui = TerminalUI(console, glyphs=glyphs, spinner=settings.ui.spinner)
    runtime = ConversationRuntime(
        llm=client,
        settings=settings,
        workspace=workspace,
        behavior=behavior,
        ui=ui,
        audit=audit,
        session=session,
        memory=memory,
    )
    if restored is not None:
        runtime.restore_history(restored.messages)
    dispatcher = SlashDispatcher(
        runtime=runtime,
        client=client,
        console=console,
        loaded=loaded,
        user_config_file=user_file,
        reload_config=load,
        glyphs=glyphs,
    )

    console.print(banner(__version__, runtime.model, settings.runtime.security_profile))
    if restored is not None:
        console.print(
            f"[sp.dim]Resumed session {escape(restored.session_id)} "
            f"({len(restored.messages)} messages).[/sp.dim]"
        )
        audit.write("session_resume", summary=restored.session_id)
    reader = make_input(console, paths.state_dir, command_words(), glyphs)

    while True:
        status = runtime.status()
        context = PromptContext(
            workspace=status.workspace, model=status.model, profile=status.profile
        )
        console.print()
        try:
            line = reader.read(context)
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("[sp.dim](Ctrl-C — use /exit to quit)[/sp.dim]")
            continue
        if not line:
            continue
        if line.startswith("/"):
            action = dispatcher.handle(line)
            if action is SlashAction.EXIT:
                break
            if action is SlashAction.MANUAL_SHELL:
                manual_shell_loop(console, workspace, audit)
            continue
        try:
            runtime.run_turn(line)
        except KeyboardInterrupt:
            ui.show_status("Interrupted.")
        except OllamaError as exc:
            ui.show_error(f"Model call failed: {exc}")
    audit.write("session_end")
    return 0
