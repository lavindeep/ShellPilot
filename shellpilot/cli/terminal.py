"""Interactive terminal session: rich rendering plus the REPL loop."""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from prompt_toolkit.application import get_app
from prompt_toolkit.application.run_in_terminal import run_in_terminal
from rich.console import Console
from rich.markup import escape
from rich.padding import Padding
from rich.text import Text

from shellpilot.cli.app import StatusValues
from shellpilot.cli.app_approval import ApprovalGate
from shellpilot.cli.app_main import run_app
from shellpilot.cli.app_slash import SlashRouter
from shellpilot.cli.app_turn import ThreadedUI, TurnRunner
from shellpilot.cli.app_ui import AppUI
from shellpilot.cli.attachments import AttachmentError, AttachmentQueue, load_image
from shellpilot.cli.banner import render_banner
from shellpilot.cli.input import PromptContext, make_input
from shellpilot.cli.manual_shell import (
    manual_shell_loop,
    run_manual_command,
    run_manual_command_captured,
)
from shellpilot.cli.model_picker import (
    choose_model,
    confirm_last_model,
    resolve_preselect,
    should_show_picker,
)
from shellpilot.cli.render import (
    _sanitize_line,
    approval_choices,
    approval_cwd,
    approval_info,
    plan_choices,
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
from shellpilot.cli.slash import (
    SlashAction,
    SlashDispatcher,
    _default_confirm,
    command_words,
)
from shellpilot.cli.status_bar import ctx_percent
from shellpilot.cli.streaming import AviationSpinner, DiffReveal, ResponseStream
from shellpilot.cli.theme import UNICODE_GLYPHS, Glyphs, build_console, resolve_glyphs
from shellpilot.config.loader import HIGH_STAKES_KEYS, ConfigError, LoadedConfig, load_config
from shellpilot.config.model import Settings, is_cloud_model, is_egressing
from shellpilot.config.overrides import load_overrides, overrides_path
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
from shellpilot.policy.approvals import ApprovalReply, ApprovalRequest
from shellpilot.policy.risk import RiskLevel
from shellpilot.runtime.conversation import ConversationRuntime
from shellpilot.runtime.events import RuntimeUI, TurnStats
from shellpilot.runtime.planner import TaskPlan
from shellpilot.skills.loader import discover_skills
from shellpilot.tools.base import workspace_display


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
        workspace: Path | None = None,
        workspace_fn: Callable[[], Path] | None = None,
    ) -> None:
        self._console = console
        self._glyphs = glyphs
        # Workspace for display-integrity (design section 14.5): when set, a
        # `path` argument in the tool-call line is shown as its resolved,
        # workspace-relative target — the SAME resolution the tool acts on.
        # workspace_fn (preferred in production) is called at render time so a
        # mid-session /cwd change is immediately reflected; workspace is the
        # static fallback for test doubles that construct without a live runtime.
        self._workspace = workspace
        self._workspace_fn = workspace_fn
        self._stream = ResponseStream(console)
        self._spinner = AviationSpinner(console, glyphs, enabled=spinner)
        # The diff-reveal animation rides the same motion toggle as the spinner.
        self._diff_reveal = DiffReveal(console, glyphs, enabled=spinner)

    def begin_response(self) -> None:
        self._spinner.start()

    def end_response(self) -> None:
        self._spinner.stop()
        self._stream.finish()

    def turn_finished(self, stats: TurnStats) -> None:
        # Context utilization now lives in the always-on bottom status bar
        # (design section 32), so the turn no longer prints a per-response line.
        # The method stays on the RuntimeUI protocol for the runtime to call.
        return

    def stream_token(self, token: str) -> None:
        self._spinner.stop()
        self._stream.feed(token)

    def stream_thinking(self, text: str) -> None:
        """No-op: the current terminal UI does not surface reasoning text.

        A later full-screen UI consumes this hook; keeping it silent here keeps
        a default session byte-identical.
        """
        return

    def show_status(self, text: str) -> None:
        self._console.print(f"[sp.dim]{escape(_sanitize_line(text))}[/sp.dim]")

    def show_error(self, text: str) -> None:
        self._spinner.stop()
        self._console.print(f"[sp.error]{escape(_sanitize_line(text))}[/sp.error]")

    def show_tool_call(self, name: str, arguments: dict[str, object]) -> None:
        # Redact secrets in the summary line so auto-approved tool calls never
        # expose credentials in the visible terminal channel. A `path` argument
        # is shown as its resolved, workspace-relative target (the SAME
        # resolution the tool acts on) so the displayed path cannot be spoofed
        # and matches the file actually touched (design section 14.5); the
        # approval panel applies the identical rule via executor._display_for.
        redacted = redact_structure(arguments)
        assert isinstance(redacted, dict)
        summary = ", ".join(
            f"{key}={self._tool_call_value(key, value)}" for key, value in redacted.items()
        )
        if len(summary) > 80:
            summary = summary[:79] + self._glyphs.ellipsis
        self._console.print(render_tool_call(name, summary, self._glyphs))
        label = Text.assemble(("running ", "sp.dim"), (_sanitize_line(name), "sp.emph"))
        self._spinner.start(label=label)

    def _tool_call_value(self, key: str, value: object) -> str:
        # A `path` argument is shown as its resolved, workspace-relative target
        # (display-integrity, design section 14.5). Prefer the live workspace
        # (workspace_fn, set in production) so a mid-session /cwd is honoured;
        # fall back to the build-time workspace, then verbatim. NOTE: the verbatim
        # fallback only happens with neither set — a test-double construction; in
        # production workspace_fn is always wired, so the path display never drifts
        # from the action.
        workspace = self._workspace_fn() if self._workspace_fn is not None else self._workspace
        if key == "path" and isinstance(value, str) and workspace is not None:
            return repr(workspace_display(workspace, value))
        return repr(value)

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

    def ask_approval(self, request: ApprovalRequest) -> ApprovalReply:
        """Badge-block approval (section 31.5); high risk requires typing 'run'.

        Three-way outcome (section 14.6): [y]es runs, [n]o declines, [e]dit
        rejects-and-steers — the proposed action is NOT run; the user types
        guidance that is fed back to the model, which re-proposes a corrected
        action through the normal gate. Empty guidance is treated as a plain
        decline (nothing runs). The HIGH-risk typed-"run" confirm is unchanged:
        only the literal "run" executes; [e] steers without running.

        No head line here: the tool-call line printed just before the approval
        already names the action, so repeating it would duplicate output.
        """
        # LIVE-ORDERING (load-bearing): stop the spinner FIRST. It joins its
        # thread and stops its Live before returning, so DiffReveal's Live (below)
        # never overlaps it — two concurrent rich Live on one Console corrupt the
        # display. The reveal must start strictly after this call.
        self._spinner.stop()
        self._console.print()
        if request.diff:
            cap = DiffReveal.WINDOW_ROWS
            # Long diffs scroll-reveal (motion only when enabled+TTY) then settle
            # into a capped window; short diffs print the full panel unchanged.
            long_diff = self._diff_reveal.reveal(request.diff, max_rows=cap)
            self._console.print(
                Padding(
                    render_diff(request.diff, self._glyphs, max_rows=cap if long_diff else None),
                    (0, 0, 0, 2),
                )
            )
        self._console.print(approval_info(request, plain_badge=self._plain_badges()))
        self._console.print(approval_cwd(request))
        try:
            # The typed-"run" gate guards HIGH-risk *commands* only. A HIGH-risk
            # tool is a sensitive-path read (design section 15): it gets the
            # standard prompt, with the classifier reason already shown above.
            if request.risk is RiskLevel.HIGH and request.kind == "command":
                answer = self._console.input(approval_choices(request)).strip()
                if answer.lower() == "run":
                    return ApprovalReply(approved=True)
                if answer.lower() in ("e", "edit"):
                    return self._read_steer()
                return ApprovalReply(approved=False)
            answer = self._console.input(approval_choices(request)).strip().lower()
            if answer in ("y", "yes"):
                return ApprovalReply(approved=True)
            if answer in ("e", "edit"):
                return self._read_steer()
            return ApprovalReply(approved=False)
        except (EOFError, KeyboardInterrupt):
            return ApprovalReply(approved=False)

    def _read_steer(self) -> ApprovalReply:
        """Read one line of steering guidance; empty input = plain decline."""
        guidance = self._console.input("  Tell the model what to do instead:\n  > ").strip()
        return ApprovalReply(approved=False, steer_text=guidance or None)

    def ask_plan_approval(self, plan: TaskPlan, path: str) -> tuple[str, str]:
        self._spinner.stop()
        self._console.print()
        self._console.print(plan_panel(plan, self._glyphs))
        self._console.print(f"[sp.faint]{escape(path)}[/sp.faint]")
        try:
            while True:
                answer = self._console.input(plan_choices()).strip().lower()
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


def high_stakes_override_notice(config_dir: Path) -> str | None:
    """One amber line naming any active high-stakes (egress/safety) override.

    A confirmed ``/config set`` for an egress/safety key persists in
    ``overrides.json`` and silently outranks ``config.toml`` every future boot,
    so surface it on each launch — egress must not quietly stay enabled. Returns
    ``None`` when no such override is present. Pure (no I/O beyond the read) so
    it is unit-testable; it gates nothing — the cloud-consent gate (§15.2) is
    the egress boundary.
    """
    overrides, _ = load_overrides(overrides_path(config_dir))
    active = {k: v for k, v in overrides.items() if k in HIGH_STAKES_KEYS}
    if not active:
        return None
    pairs = ", ".join(f"{k}={active[k]!r}" for k in sorted(active))
    return (
        f"⚠ Active overrides: {pairs} — these override config.toml; /config unset <key> to revert."
    )


def _relative_age(mtime: float, *, now: float | None = None) -> str:
    """Compact relative age, e.g. "just now", "39m ago", "2h ago", "3d ago"."""
    delta = max(0.0, (time.time() if now is None else now) - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _decline(_prompt: str) -> bool:
    """Loop-path confirm safety net (§31.17): a slash form misclassified as
    fast (display-only) can never block the event loop on ``input()`` — it
    declines instead. The terminal path uses the real ``_default_confirm``."""
    return False


def run_interactive(
    workspace: Path,
    resume: str | None = None,
    model_override: str | None = None,
    *,
    legacy_ui: bool = False,
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
    _override_notice = high_stakes_override_notice(user_file.parent)
    if _override_notice is not None:
        console.print(escape(_override_notice), style="sp.warn")
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

    # Fail fast on a missing --resume target BEFORE any consent prompt or model
    # load: resolving the session file is pure filesystem (no egress), so a
    # typo'd or stale --resume must not make the user consent and wait through a
    # preload only to then hit "session not found".
    sessions_dir = SessionStore.sessions_dir(workspace)
    resume_path: Path | None = None
    if resume is not None:
        resume_path = (
            SessionStore.latest(sessions_dir)
            if resume == "latest"
            else SessionStore.find(sessions_dir, resume)
        )
        if resume_path is None:
            console.print(
                f"[red]No saved session to resume[/red] "
                f"({'none found' if resume == 'latest' else resume}) in {sessions_dir}."
            )
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

    # Capture prior sessions for the banner BEFORE this session's meta is
    # written, so the current (empty) session never lists itself. sessions_dir
    # and the --resume existence check were resolved earlier (before consent).
    recent_sessions = [
        (label, _relative_age(mtime)) for label, mtime in SessionStore.recent(sessions_dir)
    ]
    restored = SessionStore.load(resume_path) if resume_path is not None else None
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

    # The boot banner (built once, used by both UIs). In app mode it is seeded as
    # the pane's first renderable so it shows inside the alt-screen; the legacy
    # REPL console.prints the same Panel below.
    banner = render_banner(
        chosen,
        is_cloud=egressing_session,
        profile=settings.runtime.security_profile,
        skills=settings.skills.enabled,
        recent_sessions=recent_sessions,
    )

    # Full-screen app (design section 31.13) is the default for an interactive
    # TTY; the legacy line-based REPL is opt-out via --legacy-ui, the
    # SHELLPILOT_UI=legacy env var, or any non-TTY (piped/redirected) session,
    # which the full-screen app cannot drive. In app mode the conversation is
    # driven through the marshaling ThreadedUI from the start, so its plan tools
    # capture the marshaling UI's bound methods at construction.
    use_legacy = legacy_ui or env.get("SHELLPILOT_UI") == "legacy"
    app_mode = tty and not use_legacy
    app_ui: AppUI | None = None
    app_runner: TurnRunner | None = None
    approval_gate: ApprovalGate | None = None
    ui: RuntimeUI
    if app_mode:
        app_ui = AppUI(
            glyphs=glyphs,
            workspace=workspace,
            workspace_fn=lambda: runtime.status().workspace,
            width_fn=lambda: get_app().output.get_size().columns,
            show_reasoning=settings.ui.show_reasoning_summary,
            intro=banner,
        )
        app_runner = TurnRunner(inner_ui=app_ui)
        # The focus-swap gate handles the two blocking approval methods (§31.16):
        # the worker blocks on a Future while the dock reads the user's answer.
        approval_gate = ApprovalGate(ui=app_ui, schedule=app_runner.schedule, glyphs=glyphs)
        ui = ThreadedUI(inner=app_ui, schedule=app_runner.schedule, approval_gate=approval_gate)
    else:
        ui = TerminalUI(
            console,
            glyphs=glyphs,
            spinner=settings.ui.spinner,
            workspace=workspace,
            workspace_fn=lambda: runtime.status().workspace,
        )
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
        # Audited at restore time so BOTH UIs record it (app mode returns before the
        # legacy tail); the legacy REPL still prints its own "Resumed session" notice.
        audit.write("session_resume", summary=restored.session_id)
        restored_plan = runtime.plan_manager.active
        if restored_plan is not None:
            console.print(plan_panel(restored_plan, glyphs))
            tid = escape(restored_plan.task_id)
            console.print(f"[sp.dim]Active plan restored: {tid} ({restored_plan.status}).[/sp.dim]")

    # Slash dispatcher + attachment queue: shared by BOTH the full-screen app
    # (the SlashRouter dispatches against this one instance, §31.17) and the
    # default REPL below. Hoisted above the app-mode hand-off so the router can
    # close over it; every dep was resolved earlier in run_interactive.
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

    # Hand off to the full-screen app loop (opt-in, design section 31.13). Placed
    # after the conversation + restore so the worker-thread turn drives the same
    # fully-configured runtime; the default REPL below is reached only when
    # app_mode is False, so it stays byte-identical.
    if app_mode:
        assert app_runner is not None and app_ui is not None and approval_gate is not None
        runner = app_runner  # non-optional local for the closures/lambdas below
        real_console = console

        def dispatch(line: str, target: Console) -> SlashAction:
            # Run the ONE dispatcher against the given console. Loop path (a
            # capturing console, not real_console) gets the _decline confirm so a
            # misclassified confirm-caller can never block the event loop;
            # terminal path gets the real blocking confirm. Restore in finally.
            on_loop = target is not real_console
            dispatcher._console = target
            dispatcher._confirm = _decline if on_loop else _default_confirm
            try:
                return dispatcher.handle(line)
            finally:
                dispatcher._console = real_console
                dispatcher._confirm = _default_confirm

        def app_manual_shell(line: str) -> None:
            # Bare `!` / `/shell` → the interactive manual-shell loop on the real
            # terminal. A one-shot `!<cmd>` no longer reaches here — the router runs
            # it captured on the worker via app_run_shell and renders the output in
            # the pane (§31.17). Live workspace so a prior /cwd set is honoured.
            manual_shell_loop(console, runtime.status().workspace, audit)

        def app_run_shell(command: str) -> tuple[int, str]:
            # One-shot `!<cmd>` for app mode: run captured against the LIVE
            # workspace (honours a prior /cwd); the router renders (exit, output).
            return run_manual_command_captured(command, runtime.status().workspace, audit)

        def schedule_terminal(fn: Callable[[], None]) -> None:
            # Suspend the app, run fn synchronously on the real terminal, redraw.
            # Fire-and-forget: the returned Future is intentionally not awaited.
            run_in_terminal(fn)

        router = SlashRouter(
            ui=app_ui,
            dispatch=dispatch,
            real_console=console,
            width_fn=lambda: get_app().output.get_size().columns,
            run_terminal=schedule_terminal,
            run_worker=runner.start_action,
            schedule=runner.schedule,
            manual_shell=app_manual_shell,
            run_shell=app_run_shell,
            on_exit=lambda: get_app().exit(),
            is_busy=lambda: runner.busy,
            glyphs=glyphs,
        )

        def _status_values() -> StatusValues:
            # Live status-bar inputs, one runtime.status() per render (§31.18).
            st = runtime.status()
            return StatusValues(
                workspace=st.workspace,
                model=runtime.model,
                profile=settings.runtime.security_profile,
                is_cloud=is_egressing(runtime.model, settings.model.base_url),
                ctx_pct=ctx_percent(st.estimated_prompt_tokens, st.budget.model_context_tokens),
            )

        try:
            rc = run_app(
                runtime,
                app_runner,
                app_ui,
                workspace=workspace,
                model=runtime.model,
                profile=settings.runtime.security_profile,
                glyphs=glyphs,
                commands=command_words(),
                is_cloud=egressing_session,
                ctx_pct=ctx_percent(
                    runtime.status().estimated_prompt_tokens,
                    runtime.status().budget.model_context_tokens,
                ),
                approval_gate=approval_gate,
                on_slash=router.route,
                is_busy=lambda: runner.busy,
                register_idle=lambda cb: setattr(app_runner, "on_idle", cb),
                status_fn=_status_values,
            )
        finally:
            # Mirror the legacy REPL: the session is audited as ended once the app
            # loop exits — in a finally so an unexpected app crash still records it
            # (run_app itself stays UI-only; the audit logger lives here).
            audit.write("session_end")
        return rc

    console.print(banner)
    if restored is not None:
        console.print(
            f"[sp.dim]Resumed session {escape(restored.session_id)} "
            f"({len(restored.messages)} messages).[/sp.dim]"
        )
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
        # Persistent, unspoofable active-cloud indicator (design section 15.2),
        # now folded into the always-on status bar rather than a separate header.
        # Derived from the harness egress signal on the LIVE model, so a
        # mid-session /model use to a cloud model turns it on and switching back
        # turns it off. ctx% is the same calculation the runtime reports.
        egressing_now = is_egressing(runtime.model, settings.model.base_url)
        context = PromptContext(
            workspace=status.workspace,
            model=status.model,
            profile=status.profile,
            is_cloud=egressing_now,
            ctx_pct=ctx_percent(status.estimated_prompt_tokens, status.budget.model_context_tokens),
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
        # The `!` and `/` branches run outside the normal-turn guard below, so
        # a Ctrl+C (or model error) here is caught at this level — otherwise it
        # escapes run_interactive and crashes the REPL, skipping session_end.
        try:
            if line.startswith("!"):
                # `!<cmd>` runs one command through the audited manual-shell path
                # (raw shell=True, exactly like /shell); a bare `!` opens the
                # shell loop. This is a human-only escape — model output never
                # reaches this reader — so it carries the same trust as /shell.
                # Live workspace honours a prior /cwd.
                workspace = runtime.status().workspace
                command = line[1:].strip()
                if command:
                    run_manual_command(command, workspace, audit)
                else:
                    manual_shell_loop(console, workspace, audit)
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
        except KeyboardInterrupt:
            ui.show_status("Interrupted.")
            continue
        except OllamaError as exc:
            ui.show_error(f"Model call failed: {exc}")
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
