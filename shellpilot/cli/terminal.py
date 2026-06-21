"""Interactive terminal session: rich rendering plus the REPL loop."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape
from rich.padding import Padding
from rich.text import Text

from shellpilot.cli.attachments import AttachmentError, AttachmentQueue, load_image
from shellpilot.cli.banner import render_banner
from shellpilot.cli.input import PromptContext, make_input
from shellpilot.cli.manual_shell import manual_shell_loop
from shellpilot.cli.model_picker import (
    choose_model,
    confirm_last_model,
    resolve_preselect,
    should_show_picker,
)
from shellpilot.cli.render import (
    _sanitize_line,
    approval_cwd,
    approval_info,
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
from shellpilot.config.model import Settings, is_cloud_model, is_egressing
from shellpilot.llm.ollama import OllamaClient, OllamaError
from shellpilot.memory.agents_md import (
    BehaviorInstructions,
    load_behavior_instructions,
    project_agents_md_digest,
)
from shellpilot.memory.redaction import redact_structure
from shellpilot.memory.store import MemoryFormatError, MemoryStore, MemoryStores, project_id_for
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.persistence.paths import AppPaths, project_state_dir
from shellpilot.persistence.sessions import SessionStore
from shellpilot.persistence.workspace_state import (
    load_last_model,
    load_trusted_agents_digest,
    save_last_model,
    save_trusted_agents_digest,
)
from shellpilot.policy.approvals import ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.events import TurnStats
from shellpilot.runtime.planner import TaskPlan
from shellpilot.skills.loader import discover_skills


def should_discard_interrupt(
    turn_just_ran: bool, elapsed_seconds: float, window_seconds: float = 0.1
) -> bool:
    """A KeyboardInterrupt during prompt input is a stale leftover from the
    just-finished turn only when it arrives almost immediately after the
    prompt starts reading; a later interrupt is the user's own."""
    return turn_just_ran and elapsed_seconds < window_seconds


def _resolve_project_agents_trust(console: Console, workspace: Path, *, tty: bool) -> bool:
    """Trust-on-first-use gate for the project ``<workspace>/AGENTS.md``.

    The project AGENTS.md is injected as standing instructions with the same
    authority as ShellPilot's own prompt, so a cloned/untrusted repo could
    silently steer the assistant. Load it only when its current digest matches
    a previously accepted one, or after the user accepts it this session.
    A non-TTY session fails closed (not loaded). Global config-dir AGENTS.md is
    unaffected — it is always trusted. (Design section 16.)
    """
    digest = project_agents_md_digest(workspace)
    if digest is None:
        return True  # no project AGENTS.md to gate
    trusted = load_trusted_agents_digest(workspace)
    if digest == trusted:
        return True
    if not tty:
        console.print("[sp.dim]Project AGENTS.md not loaded (non-interactive; untrusted).[/sp.dim]")
        return False
    note = "changed since you last trusted it" if trusted is not None else "new"
    console.print(
        f"[yellow]Project AGENTS.md[/yellow] at "
        f"[sp.faint]{escape(str(workspace / 'AGENTS.md'))}[/sp.faint] is {note}.\n"
        "[sp.dim]It would be loaded as standing instructions with the same "
        "authority as ShellPilot's own prompt.[/sp.dim]"
    )
    try:
        answer = console.input("  Trust and load it? [sp.dim]\\[y/N][/sp.dim] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        save_trusted_agents_digest(workspace, digest)
        return True
    console.print("[sp.dim]Project AGENTS.md not loaded (declined).[/sp.dim]")
    return False


# The honest disclosure shown before egressing to a cloud/remote model. It must
# state plainly what leaves the device — this prompt IS the consent boundary
# (design section 15.2). Best-effort redaction is defence-in-depth, not a
# guarantee, so the text does not promise protection.
CLOUD_CONSENT_DISCLOSURE = (
    "[yellow]{model}[/yellow] is a cloud/remote model: it runs OFF this device.\n"
    "[sp.dim]The ENTIRE prompt — file contents, command output, and any memory the "
    "model reads — is sent to the provider and may be UNREDACTED. Under the balanced "
    "profile, low-risk actions auto-run and can send data without a per-action prompt. "
    "The provider's retention, training, and jurisdiction are outside ShellPilot's "
    "control.[/sp.dim]"
)


def _resolve_cloud_consent(console: Console, settings: Settings, chosen: str, *, tty: bool) -> bool:
    """Per-session consent gate for a cloud/remote (egressing) model.

    The consent boundary for data leaving the device (design section 15.2).
    Returns True to proceed, False to abort — the caller MUST NOT touch the
    model (no preload, no metadata, no chat) when this returns False.

    Fails closed on every uncertainty:
    - A non-egressing local session proceeds with NO prompt (the common path).
    - An egressing session with ``allow_cloud`` off is refused with a clear
      message pointing at the config switch.
    - An egressing session in a non-interactive (non-TTY) context is refused
      — there is no way to obtain consent, so nothing egresses.
    - Otherwise the user is shown an honest disclosure and a y/N prompt that
      DEFAULTS TO NO; only an explicit yes proceeds (Enter/EOF/no decline).

    Consent is per session — never persisted; every launch re-asks.
    """
    egressing = is_egressing(chosen, settings.model.base_url)
    if not egressing:
        return True
    if not settings.model.allow_cloud:
        console.print(
            f"[red]{escape(chosen)} is a cloud/remote model; cloud egress is off.[/red] "
            "Set [model] allow_cloud = true in config.toml to enable."
        )
        return False
    if not tty:
        console.print(
            "[red]Cloud model requires interactive consent; refusing (non-interactive).[/red]"
        )
        return False
    console.print(CLOUD_CONSENT_DISCLOSURE.format(model=escape(chosen)))
    try:
        answer = console.input("  Send this session to the cloud? [sp.dim]\\[y/N][/sp.dim] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    console.print("[sp.dim]Cloud model declined; not started.[/sp.dim]")
    return False


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
        self._console.print(f"[sp.dim]{escape(_sanitize_line(text))}[/sp.dim]")

    def show_error(self, text: str) -> None:
        self._spinner.stop()
        self._console.print(f"[sp.error]{escape(_sanitize_line(text))}[/sp.error]")

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        # Redact secrets in the summary line so auto-approved tool calls never
        # expose credentials in the visible terminal channel.  The approval
        # panel (ApprovalRequest.display built by executor._display_for) is
        # intentionally left raw: the user approves exactly what will execute.
        redacted = redact_structure(arguments)
        assert isinstance(redacted, dict)
        summary = ", ".join(f"{key}={value!r}" for key, value in redacted.items())
        if len(summary) > 80:
            summary = summary[:79] + self._glyphs.ellipsis
        self._console.print(render_tool_call(name, summary, self._glyphs))
        label = Text.assemble(("running ", "sp.dim"), (_sanitize_line(name), "sp.emph"))
        self._spinner.start(label=label)

    def show_tool_result(self, name: str, success: bool, summary: str) -> None:
        self._spinner.stop()
        self._console.print(render_tool_result(success, summary, self._glyphs))

    def show_command_output(self, line: str) -> None:
        self._spinner.stop()
        self._console.print(
            "    " + _sanitize_line(line), style="sp.dim", markup=False, highlight=False
        )

    def show_plan_progress(self, plan: TaskPlan) -> None:
        self._spinner.stop()
        for index, step in enumerate(plan.steps, 1):
            self._console.print(Padding(plan_step_line(index, step, self._glyphs), (0, 0, 0, 2)))
        self._console.print()

    def _plain_badges(self) -> bool:
        return self._console.no_color or not self._console.is_terminal

    def ask_approval(self, request: ApprovalRequest) -> bool:
        """Badge-block approval (section 31.5); high risk requires typing 'run'.

        No head line here: the tool-call line printed just before the approval
        already names the action, so repeating it would duplicate output.
        """
        self._spinner.stop()
        self._console.print()
        if request.diff:
            self._console.print(Padding(render_diff(request.diff, self._glyphs), (0, 0, 0, 2)))
        self._console.print(approval_info(request, plain_badge=self._plain_badges()))
        self._console.print(approval_cwd(request))
        try:
            # The typed-"run" gate guards HIGH-risk *commands* only. A HIGH-risk
            # tool is a sensitive-path read (design section 15): it gets the
            # standard y/n prompt, with the classifier reason already shown above.
            if request.risk is RiskLevel.HIGH and request.kind == "command":
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
        self._spinner.stop()
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


def run_interactive(
    workspace: Path, resume: str | None = None, model_override: str | None = None
) -> int:
    console = build_console(Settings())
    env = dict(os.environ)
    paths = AppPaths.default()
    user_file, project_file = config_files(workspace, env, paths)
    cli_overrides = {"model.default": model_override} if model_override is not None else None

    def load() -> LoadedConfig:
        return load_config(
            user_config_file=user_file,
            project_config_file=project_file,
            env=env,
            cli_overrides=cli_overrides,
        )

    try:
        loaded = load()
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {escape(str(exc))}")
        return 2
    for _warning in loaded.warnings:
        console.print(f"[dim]{escape(_warning)}[/dim]")
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
    installed_models = client.list_models()
    installed = {m.name for m in installed_models}

    tty = console.is_terminal and sys.stdin.isatty()
    if not should_show_picker(
        tty=tty,
        model_override=model_override,
        installed_count=len(installed_models),
    ):
        chosen = settings.model.default
    else:
        last = load_last_model(workspace)
        if last is not None and last in installed:
            # Every boot after the first: Enter flies the last model, any other
            # key opens the full menu (preselected on the last model).
            if confirm_last_model(console, last):
                chosen = last
            else:
                chosen = choose_model(console, installed_models, last)
        else:
            # First boot, or the last model is no longer installed.
            chosen = choose_model(
                console,
                installed_models,
                resolve_preselect(settings.model.default, last, installed),
            )
        save_last_model(workspace, chosen)

    # Cloud models are absent from the local /api/tags, so the availability gate
    # is skipped for them (the typo-catch survives for local names).
    if chosen not in installed and not is_cloud_model(chosen):
        console.print(f"[red]Model {chosen} is not installed.[/red] Try: ollama pull {chosen}")
        return 1

    # Cloud-egress consent boundary (design section 15.2): a cloud/remote model
    # must clear allow_cloud + per-session consent BEFORE any prompt-bearing call
    # touches it. Placed strictly before _preload — the first egress point — so a
    # declined session performs no model load and no chat. (client.health/
    # list_models above hit /api/tags on base_url only: for the primary
    # cloud-model case base_url is loopback → no egress; a non-loopback base_url
    # is a metadata-only probe to the user's own configured endpoint, documented
    # as an accepted residual in DESIGN §15.2.)
    egressing_session = is_egressing(chosen, settings.model.base_url)
    if not _resolve_cloud_consent(console, settings, chosen, tty=tty):
        return 1

    # ------------------------------------------------------------------
    # A9/A10: warm the chosen model into memory before the first turn.
    # Spinner shows "fueling <model>"; errors are best-effort warnings.
    # ------------------------------------------------------------------
    _boot_spinner = AviationSpinner(console, glyphs, enabled=settings.ui.spinner)

    def _preload(model_name: str) -> None:
        label = Text.assemble(("fueling ", "sp.dim"), (model_name, "sp.emph"))
        _boot_spinner.start(label=label)
        try:
            client.preload(model_name, keep_alive=settings.model.keep_alive)
        except OllamaError as exc:
            msg = f"Warning: model preload failed: {escape(str(exc))}"
            console.print(f"[sp.dim][yellow]{msg}[/yellow][/sp.dim]")
        finally:
            _boot_spinner.stop()

    _preload(chosen)

    detected = client.model_context_length(chosen)
    ctx = detected or 8192
    if settings.instructions.load_agents_md:
        cap = min(1500, ctx // 10)
        project_trusted = _resolve_project_agents_trust(console, workspace, tty=tty)
        behavior = load_behavior_instructions(
            paths.config_dir, workspace, max_tokens=cap, project_trusted=project_trusted
        )
    else:
        behavior = BehaviorInstructions(global_text=None, project_text=None)

    skills_cap = min(800, ctx // 12)
    discovered_skills = discover_skills(
        user_skills_dir=paths.config_dir / "skills",
        max_tokens=skills_cap,
    )

    audit = AuditLogger(
        path=paths.state_dir / "audit.jsonl",
        session_id=uuid.uuid4().hex[:12],
        workspace=workspace,
        profile=settings.runtime.security_profile,
        redact=settings.privacy.redact_secrets,
    )
    audit.write("session_start", model=chosen)
    if egressing_session:
        # Record that the user granted cloud-egress consent for this session
        # (consent already happened above; logged now that the logger exists).
        audit.write(
            "cloud_consent_granted",
            model=chosen,
            host=(urlsplit(settings.model.base_url).hostname or "").rstrip("."),
        )

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
        model=chosen,
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
        model=chosen,
        audit=audit,
        session=session,
        memory=memory,
        skills=discovered_skills,
        base_url=settings.model.base_url,
    )
    if restored is not None:
        runtime.restore_history(restored.messages)
        runtime.restore_active_plan(restored.active_plan_task_id)
        restored_plan = runtime.plan_manager.active
        if restored_plan is not None:
            console.print(plan_panel(restored_plan, glyphs))
            tid = escape(restored_plan.task_id)
            console.print(f"[sp.dim]Active plan restored: {tid} ({restored_plan.status}).[/sp.dim]")
    attachments = AttachmentQueue()
    dispatcher = SlashDispatcher(
        runtime=runtime,
        client=client,
        console=console,
        loaded=loaded,
        user_config_file=user_file,
        reload_config=load,
        glyphs=glyphs,
        preload=_preload,
        attachments=attachments,
        tty=tty,
    )

    console.print(render_banner(runtime.model, is_cloud=egressing_session))
    if restored is not None:
        console.print(
            f"[sp.dim]Resumed session {escape(restored.session_id)} "
            f"({len(restored.messages)} messages).[/sp.dim]"
        )
        audit.write("session_resume", summary=restored.session_id)
    reader = make_input(console, paths.state_dir, command_words(), glyphs)

    # When a turn completes (normally or via the inner KeyboardInterrupt handler)
    # a buffered SIGINT can be delivered to the very next reader.read() call,
    # firing the outer KeyboardInterrupt handler and printing the hint AFTER the
    # prompt has already appeared.  _turn_just_ran tracks this: set to True after
    # every turn dispatch so the outer handler silently discards the stale
    # interrupt instead of printing the hint.
    _turn_just_ran = False

    while True:
        status = runtime.status()
        context = PromptContext(
            workspace=status.workspace, model=status.model, profile=status.profile
        )
        # Persistent, unspoofable active-cloud indicator (design section 15.2).
        # Derived from the harness egress signal on the LIVE model, so a
        # mid-session /model use to a cloud model turns it on and switching back
        # turns it off. A default local session prints nothing.
        if is_egressing(runtime.model, settings.model.base_url):
            console.print(
                Text(
                    "☁ CLOUD MODEL ACTIVE — this session's content leaves your device",
                    style="bold sp.warn",
                )
            )
        console.print()
        read_started = time.monotonic()
        try:
            line = reader.read(context)
            _turn_just_ran = False
        except EOFError:
            break
        except KeyboardInterrupt:
            elapsed = time.monotonic() - read_started
            if should_discard_interrupt(_turn_just_ran, elapsed):
                # Stale buffered SIGINT from a just-completed turn — discard silently.
                _turn_just_ran = False
            else:
                console.print("[sp.dim](Ctrl-C — use /exit to quit)[/sp.dim]")
            continue
        if not line:
            continue
        if line.startswith("/"):
            action = dispatcher.handle(line)
            if action is SlashAction.EXIT:
                break
            if action is SlashAction.MANUAL_SHELL:
                # Fetch the live workspace from the runtime so that a prior
                # /cwd set is honoured, rather than using the stale local
                # captured at startup.
                manual_shell_loop(console, runtime.status().workspace, audit)
            continue
        try:
            staged_paths = attachments.take()
            refs = []
            for p in staged_paths:
                try:
                    refs.append(load_image(p))
                except AttachmentError as exc:
                    ui.show_status(f"Attachment dropped ({p.name}): {exc}")
            runtime.run_turn(line, images=tuple(refs))
        except KeyboardInterrupt:
            ui.show_status("Interrupted.")
        except OllamaError as exc:
            ui.show_error(f"Model call failed: {exc}")
        finally:
            _turn_just_ran = True
    audit.write("session_end")
    return 0
