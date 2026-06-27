"""Unified conversation runtime (design section 10): one loop, no chat/agent split."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from shellpilot.config.model import Settings, is_egressing
from shellpilot.llm.client import LLMClient
from shellpilot.llm.messages import ImageRef, Message, tool_result, user
from shellpilot.llm.ollama import encode_tool, is_loopback_url
from shellpilot.memory.agents_md import BehaviorInstructions
from shellpilot.memory.redaction import redact_secrets, redact_structure
from shellpilot.memory.store import MemoryStore, MemoryStores, project_id_for
from shellpilot.persistence.audit_store import AuditLogger
from shellpilot.persistence.paths import project_state_dir
from shellpilot.persistence.sessions import SessionStore
from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.policy.risk import SideEffect
from shellpilot.prompts.system import build_system_prompt
from shellpilot.runtime.budget import ContextBudget, estimate_tokens, resolve_budget
from shellpilot.runtime.context import ContextAssembler, ContextSnapshot
from shellpilot.runtime.events import RuntimeUI, TurnStats
from shellpilot.runtime.executor import ExecutionOutcome, ToolExecutor
from shellpilot.runtime.planner import PlanManager, TaskPlan, compact_plan_state, make_plan_tools
from shellpilot.skills.model import Skill
from shellpilot.skills.triggers import TriggerContext
from shellpilot.tools.images import make_view_image_tool
from shellpilot.tools.registry import ToolRegistry, default_registry

# Coarse per-image vision-encoder cost; deliberately NOT the b64 length
# (that would wildly overestimate and thrash compaction).
IMAGE_TOKEN_ESTIMATE = 1024

MIN_KEPT_MESSAGES = 4
MAX_CONSECUTIVE_MALFORMED = 2
TOOL_DIGEST_HEAD = 200
TOOL_DIGEST_TAIL = 200
MAX_PLAN_NUDGES = 2
MAX_EMPTY_NUDGES = 2
# NOTE: tunable ceiling separating a real end-of-plan summary from a terse
# "done." When a completing reply already carries content this long, the streamed
# prose IS the single summary and the redundant re-summary round is skipped.
MIN_SUMMARY_CHARS = 80

EMPTY_CONTINUE_NUDGE = (
    "Your last reply was empty — no text and no tool call. You have already run a "
    "tool this turn, so do not stop here. Either call the next tool to continue, or "
    "write your answer in plain text now. Do not reply with an empty message."
)

EMPTY_FIRST_NUDGE = (
    "Your last reply was empty — no text and no tool call. Answer the user's "
    "request now: reply in plain text, or call a tool if you need more "
    "information. Do not reply with an empty message."
)

PLAN_CONTINUE_NUDGE = (
    "The approved plan is not finished (next step {index}: {title}). If you have "
    "completed this step, record it now with update_plan(step={index}, "
    'status="completed") and continue to the next step in this same turn. If the '
    "step still needs work, do it now with the appropriate tool, then record "
    "completion. If something is blocking you, record it with "
    'update_plan(blocker="<evidence>"). Only if you need information that the user '
    "alone can provide: ask the user plainly and stop."
)


class _Unset:
    """Sentinel type: distinct from None, which is a valid recorded plan pointer."""


_UNSET = _Unset()


def _plan_pointer(plan: TaskPlan | None) -> str | None:
    """Map a plan to its session-pointer value: task_id for live statuses, None otherwise."""
    if plan is None:
        return None
    if plan.status in ("proposed", "active", "blocked"):
        return plan.task_id
    return None


def _digest_text(content: str) -> str:
    """Head/tail digest for an old tool result; deterministic, no model call."""
    if len(content) <= TOOL_DIGEST_HEAD + TOOL_DIGEST_TAIL + 60:
        return content
    omitted = len(content) - TOOL_DIGEST_HEAD - TOOL_DIGEST_TAIL
    return (
        content[:TOOL_DIGEST_HEAD]
        + f"\n[... {omitted} chars compacted ...]\n"
        + content[-TOOL_DIGEST_TAIL:]
    )


@dataclass(frozen=True)
class RuntimeStatus:
    """Snapshot for /status and /compact status."""

    model: str
    profile: str
    workspace: Path
    estimated_prompt_tokens: int
    budget: ContextBudget
    history_messages: int


class ConversationRuntime:
    """Owns conversation history, budgets, and model calls for one session."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        settings: Settings,
        workspace: Path,
        behavior: BehaviorInstructions,
        ui: RuntimeUI,
        model: str | None = None,
        registry: ToolRegistry | None = None,
        audit: AuditLogger | None = None,
        session: SessionStore | None = None,
        memory: MemoryStores | None = None,
        skills: Sequence[Skill] | None = None,
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._audit = audit
        self._session = session
        self._memory = memory
        self._llm = llm
        self._settings = settings
        # The model endpoint URL — the egress-locality signal. The default is
        # loopback, so every existing caller and test (FakeLLM) is non-egressing
        # and behaviour is byte-identical.
        self._base_url = base_url
        self._workspace = workspace
        self._behavior = behavior
        self._ui = ui
        self._model = model or settings.model.default
        self._registry = registry or default_registry()
        self._skills: tuple[Skill, ...] = tuple(skills) if skills is not None else ()
        self._history: list[Message] = []
        self._staged_tool_images: list[ImageRef] = []
        self._last_user_text = ""
        self._last_failure_signature: str | None = None
        self.snapshots = SnapshotStore()
        self.recent_diffs: list[str] = []
        self.plan_manager = PlanManager(workspace, settings.runtime.security_profile)
        self._last_recorded_plan_ptr: str | None | _Unset = _UNSET
        self.plan_manager.on_change = self._on_plan_change
        for spec in make_plan_tools(
            self.plan_manager,
            ui.ask_plan_approval,
            lambda: self._last_user_text,
            on_step_change=ui.show_plan_progress,
            max_plan_steps=settings.runtime.max_plan_steps,
        ):
            self._registry.register(spec)
        self._registry.register(
            make_view_image_tool(
                self._staged_tool_images.append,
                lambda: "vision" in self._llm.model_capabilities(self._model),
            )
        )
        if memory is not None:
            from shellpilot.tools.memory_tools import make_memory_tools

            for spec in make_memory_tools(memory):
                self._registry.register(spec)
        if settings.tools.web:
            from shellpilot.tools.web import default_web_tools

            for spec in default_web_tools():
                self._registry.register(spec)
        if settings.skills.enabled:
            from shellpilot.tools.skill_tools import make_skill_read_tool

            valid_skills = tuple(s for s in self._skills if s.valid)
            self._registry.register(make_skill_read_tool(valid_skills))
        self.budget = self._resolve_budget()
        self._assembler = ContextAssembler()

    @property
    def model(self) -> str:
        return self._model

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def audit(self) -> AuditLogger | None:
        return self._audit

    @property
    def session(self) -> SessionStore | None:
        return self._session

    @property
    def memory(self) -> MemoryStores | None:
        return self._memory

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._skills

    def _record(self, message: Message) -> None:
        """Append to history and the session transcript together."""
        self._history.append(message)
        if self._session is not None:
            self._session.record_message(message)

    def restore_history(self, messages: list[Message]) -> None:
        """Adopt a prior session's messages without re-recording them.

        Snapshots are deliberately not restored: read-before-write safety
        requires fresh reads in the new process.
        """
        self._history = list(messages)

    def _on_plan_change(self, plan: TaskPlan | None) -> None:
        """Deduplicated session recorder: only write a pointer when it changes."""
        ptr = _plan_pointer(plan)
        last = self._last_recorded_plan_ptr
        if not isinstance(last, _Unset) and ptr == last:
            return
        self._last_recorded_plan_ptr = ptr
        if self._session is not None:
            self._session.record_active_plan(ptr)

    def restore_active_plan(self, task_id: str | None) -> None:
        """Restore an active plan from a prior session. None → no-op."""
        if task_id is None:
            return
        from shellpilot.runtime.planner import load_plan

        plan = load_plan(self._workspace, task_id)
        if plan is None:
            return
        if plan.status not in ("proposed", "active", "blocked"):
            return
        self.plan_manager.restore(plan)
        # Prime the dedupe cache so restore doesn't trigger a new session record
        self._last_recorded_plan_ptr = task_id

    def _resolve_budget(self) -> ContextBudget:
        detected = self._llm.model_context_length(self._model)
        return resolve_budget(self._settings.context, detected)

    def set_model(self, model: str) -> None:
        self._model = model
        self.budget = self._resolve_budget()

    def set_workspace(self, workspace: Path) -> None:
        self._workspace = workspace
        self.plan_manager.set_workspace(workspace)
        if self._memory is not None:
            # Project memory is path-scoped (workspace/.shellpilot/memory.json),
            # so a /cwd change must rebuild the project store for the new path —
            # otherwise the previous workspace's facts keep injecting (and, under
            # cloud, egressing). The shared global store is preserved as-is.
            self._memory = dataclasses.replace(
                self._memory,
                project_store=MemoryStore(
                    project_state_dir(workspace) / "memory.json",
                    project_id=project_id_for(workspace),
                    redact=self._settings.privacy.redact_secrets,
                ),
            )
        if self._audit is not None:
            # Update the logger's workspace field BEFORE writing the event so
            # the change record itself (and all subsequent events) carry the new
            # path.  This is the single gateway for workspace changes, so the
            # update cannot be bypassed.
            self._audit.workspace = workspace
            self._audit.write("config_change", setting="workspace", value=str(workspace))

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings
        self.budget = self._resolve_budget()

    def _endpoint_host(self) -> str:
        """Host of the model endpoint (for audit); empty when unparseable."""
        return (urlsplit(self._base_url).hostname or "").rstrip(".")

    def _endpoint_is_loopback(self) -> bool:
        """True when the model endpoint is on this box (loopback = local).

        Delegates to the shared ``is_loopback_url`` helper so the runtime egress
        chokepoint and the CLI boot consent gate use one definition of off-box.
        """
        return is_loopback_url(self._base_url)

    def _is_egressing(self) -> bool:
        """True when a model request leaves this device.

        Delegates to the shared ``is_egressing`` predicate so the runtime egress
        chokepoint, the boot consent gate, and the active-cloud UI indicator all
        agree on what counts as off-box (design section 15.2).
        """
        return is_egressing(self._model, self._base_url)

    def _redacted_for_egress(self, messages: list[Message]) -> list[Message]:
        """Best-effort redacted COPY of *messages* for a remote send.

        Defence-in-depth, NOT a guarantee: regex redaction misses novel secret
        formats, and image/base64 data is left as-is (not redactable here — this
        is disclosed, not protected). Never mutates ``self._history``: each
        Message is rebuilt via dataclasses.replace with its content run through
        redact_secrets and any tool-call arguments through redact_structure.
        """
        out: list[Message] = []
        for message in messages:
            redacted_calls = tuple(
                dataclasses.replace(
                    call,
                    arguments=redact_structure(call.arguments),  # type: ignore[arg-type]
                )
                for call in message.tool_calls
            )
            out.append(
                dataclasses.replace(
                    message,
                    content=redact_secrets(message.content),
                    tool_calls=redacted_calls,
                )
            )
        return out

    def clear_history(self) -> None:
        self._history.clear()
        self.plan_manager.cancel()
        self.snapshots.clear()
        self.recent_diffs.clear()
        self._last_failure_signature = None
        self._last_user_text = ""
        if self._audit is not None:
            self._audit.write("clear", summary="history, plan, snapshots, diffs")
        if self._session is not None:
            self._session.record_clear()

    def _context_snapshot(self) -> ContextSnapshot:
        """Structured system-prompt snapshot — single source for the live
        prompt and the /context breakdown. Renders each block here (the
        assembler stays pure) in the order the legacy concatenation used."""
        base_prompt = build_system_prompt(
            workspace=self._workspace,
            profile=self._settings.runtime.security_profile,
            is_egressing=self._is_egressing(),
        )
        memory_block = ""
        if self._memory is not None:
            memory_cap = max(200, self.budget.model_context_tokens // 16)
            memory_block = self._memory.render(max_tokens=memory_cap)
        plan = self.plan_manager.active
        plan_state = (
            compact_plan_state(plan)
            if plan is not None and plan.status in ("active", "blocked")
            else ""
        )
        trigger_ctx = TriggerContext(
            plan_status=plan.status if plan is not None else None,
            web_enabled=(
                self._registry.get("web_search") is not None
                and self._registry.get("web_fetch") is not None
            ),
            enabled=self._settings.skills.enabled,
        )
        return self._assembler.assemble(
            base_prompt=base_prompt,
            behavior_block=self._behavior.as_prompt_block(),
            memory_block=memory_block,
            skills=self._skills,
            skill_token_budget=self.budget.model_context_tokens // 6,
            plan_state=plan_state,
            trigger_ctx=trigger_ctx,
        )

    def context_snapshot(self) -> ContextSnapshot:
        """Public accessor for the CLI (/context)."""
        return self._context_snapshot()

    def _system_message_text(self) -> str:
        return self._context_snapshot().system_text()

    def tool_schema_tokens(self) -> int:
        """Estimated tokens for the live profile's encoded tool schemas."""
        profile = self._settings.runtime.security_profile
        return sum(
            estimate_tokens(json.dumps(encode_tool(definition)))
            for definition in self._registry.definitions_for_profile(profile)
        )

    def history_token_estimate(self) -> tuple[int, int]:
        """Estimated history tokens (incl. images) and message count, counted
        exactly as estimated_prompt_tokens does for the /context breakdown."""
        total = 0
        for message in self._history:
            total += estimate_tokens(message.content)
            total += IMAGE_TOKEN_ESTIMATE * len(message.images)
        return total, len(self._history)

    def estimated_prompt_tokens(self) -> int:
        history_tokens, _ = self.history_token_estimate()
        return self._context_snapshot().est_system_tokens + history_tokens

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            model=self._model,
            profile=self._settings.runtime.security_profile,
            workspace=self._workspace,
            estimated_prompt_tokens=self.estimated_prompt_tokens(),
            budget=self.budget,
            history_messages=len(self._history),
        )

    def _over_threshold(self) -> bool:
        return self.estimated_prompt_tokens() > self.budget.compact_at_tokens

    def _old_region(self) -> int:
        """Messages before this index are compactable; the recent window is not."""
        return max(0, len(self._history) - MIN_KEPT_MESSAGES)

    def compact_now(self) -> int:
        """Selective compaction (section 20.2): cheapest context first.

        Pass 1 digests old tool results in place; pass 2 drops old non-user
        messages (an assistant tool call takes its tool results with it, so no
        orphans confuse the model); pass 3, last resort, drops the oldest user
        messages while always keeping the newest one. Plan state and snapshot
        metadata live outside history and are never touched.
        """
        changed = 0
        # Digestion may reach everything except the in-flight exchange (last 2
        # messages); snapshot staleness checks still force a fresh read before
        # any write, so losing exact tool text is safe. Drops below stay gated
        # by the wider MIN_KEPT_MESSAGES window.
        for index in range(max(0, len(self._history) - 2)):
            if not self._over_threshold():
                break
            message = self._history[index]
            if message.role == "tool":
                digest = _digest_text(message.content)
                if digest != message.content:
                    self._history[index] = Message(role="tool", content=digest)
                    changed += 1
        while self._over_threshold():
            drop_index = next(
                (i for i in range(self._old_region()) if self._history[i].role != "user"),
                None,
            )
            if drop_index is None:
                break
            removed = self._history.pop(drop_index)
            changed += 1
            if removed.tool_calls:
                # The results belong to the dropped call; orphans confuse the model.
                while drop_index < len(self._history) and self._history[drop_index].role == "tool":
                    self._history.pop(drop_index)
                    changed += 1
        while self._over_threshold():
            user_indices = [i for i, m in enumerate(self._history) if m.role == "user"]
            if len(user_indices) <= 1:
                break
            self._history.pop(user_indices[0])
            changed += 1
        return changed

    def run_turn(self, text: str, *, images: Sequence[ImageRef] = ()) -> str:
        """One user turn: budget-check, compact, call the model, stream, record."""
        # Belt-and-braces: a stale stage left by an aborted prior turn must not
        # attach to this unrelated turn's first message.
        self._staged_tool_images.clear()
        if estimate_tokens(text) > self.budget.max_user_message_tokens:
            self._ui.show_status(
                "Message too large for the model context "
                f"(limit ~{self.budget.max_user_message_tokens} tokens). "
                "Save it to a file and ask me to read it instead."
            )
            return ""

        if not self._settings.runtime.auto_compact and (
            self.estimated_prompt_tokens()
            + estimate_tokens(text)
            + IMAGE_TOKEN_ESTIMATE * len(images)
            > self.budget.hard_limit_tokens
        ):
            self._ui.show_status(
                "Context is over the hard limit and automatic compaction is off. "
                "Run /compact (or /clear), or turn it back on with /compact auto on."
            )
            return ""

        started = time.monotonic()
        self._last_user_text = text
        if self._audit is not None:
            audit_kwargs: dict[str, object] = {"chars": len(text)}
            if images:
                audit_kwargs["images"] = len(images)
            self._audit.write("user_turn", **audit_kwargs)
        self._record(user(text, images=tuple(images)))
        if self._settings.runtime.auto_compact:
            adjusted = self.compact_now()
            if adjusted:
                self._ui.show_status(f"Compacted context: adjusted {adjusted} messages.")
        content = self._tool_loop().content
        self._ui.turn_finished(self._turn_stats(time.monotonic() - started))
        return content

    def _turn_stats(self, elapsed_s: float) -> TurnStats:
        used = self.estimated_prompt_tokens()
        total = self.budget.model_context_tokens
        pct = min(100, round(100 * used / total)) if total else 0
        return TurnStats(
            elapsed_s=elapsed_s,
            context_tokens=used,
            context_pct=pct,
            warn=used > self.budget.compact_at_tokens,
        )

    def _pending_plan_step(self) -> tuple[int, str] | None:
        """First unfinished step of the active plan, as (1-based index, title).

        Returns the first step whose status is "active", else the first
        "pending" step. Returns None when there is no plan, the plan is not yet
        active (e.g. still "proposed" awaiting approval, or blocked/completed),
        or every step is already in a terminal state. Used by the tool loop to
        decide whether a no-tool-call reply should be nudged to keep executing.
        """
        plan = self.plan_manager.active
        if plan is None or plan.status != "active":
            return None
        active = next(
            (i for i, step in enumerate(plan.steps, start=1) if step.status == "active"),
            None,
        )
        if active is not None:
            return active, plan.steps[active - 1].title
        pending = next(
            (i for i, step in enumerate(plan.steps, start=1) if step.status == "pending"),
            None,
        )
        if pending is not None:
            return pending, plan.steps[pending - 1].title
        return None

    def _tool_loop(self) -> Message:
        """Model call loop with tool dispatch, budgets, and recovery (section 10.4)."""
        executor = ToolExecutor(
            registry=self._registry,
            workspace=self._workspace,
            profile=self._settings.runtime.security_profile,
            max_result_tokens=self.budget.max_tool_prompt_tokens,
            max_total_tokens=self.budget.max_total_tool_prompt_tokens,
            max_capture_chars=self.budget.max_command_capture_chars,
            command_timeout_seconds=self._settings.runtime.command_timeout_seconds,
            ask_approval=self._ui.ask_approval,
            emit_output=self._ui.show_command_output,
            snapshots=self.snapshots,
            audit=self._audit,
            allow_sensitive_reads=self._settings.privacy.allow_sensitive_reads,
        )
        tools = executor.available_definitions()
        tool_turns = 0
        nudges_used = 0
        empty_nudges_used = 0
        consecutive_malformed = 0

        while True:
            messages = [
                Message(role="system", content=self._system_message_text()),
                *self._history,
            ]
            egressing = self._is_egressing()
            if egressing and self._audit is not None:
                # Egress visibility (F10/F12): record THAT a request left the
                # device, to where, and how much — counts and host/model only,
                # never message bodies. AuditLogger stamps workspace/session/ts.
                self._audit.write(
                    "model_request",
                    host=self._endpoint_host(),
                    model=self._model,
                    locality="remote",
                    message_count=len(messages),
                    approx_bytes=sum(len(m.content or "") for m in messages),
                    image_count=sum(len(m.images) for m in messages if m.images),
                )
            # Outbound redaction (F3) — best-effort DiD applied ONLY to remote
            # turns: a loopback send is passed byte-identical (no copy). Images
            # and novel-format secrets are NOT redactable here and still egress.
            send_messages = messages
            if egressing and self._settings.privacy.redact_secrets:
                send_messages = self._redacted_for_egress(messages)
            self._ui.begin_response()
            try:
                reply = self._llm.chat(
                    self._model,
                    send_messages,
                    tools=tools,
                    num_ctx=self.budget.model_context_tokens,
                    options=self._settings.model.options,
                    on_token=self._ui.stream_token,
                )
            finally:
                self._ui.end_response()
            self._record(reply)
            if not reply.tool_calls:
                pending = self._pending_plan_step()
                if pending is not None and tools and nudges_used < MAX_PLAN_NUDGES:
                    nudges_used += 1
                    index, title = pending
                    if self._audit is not None:
                        self._audit.write("plan_nudge", summary=f"step {index}")
                    self._record(tool_result(PLAN_CONTINUE_NUDGE.format(index=index, title=title)))
                    continue
                # Any empty reply — including a thinking-only first reply (observed
                # live: 4.2k thinking tokens, zero content, zero tool calls, nothing
                # rendered) — is nudged to keep going.  Post-tool stalls and silent
                # first replies are equally unacceptable.  Plan nudge above keeps
                # priority.
                if not reply.content.strip():
                    thinking_hint = (
                        f", thinking-only reply, {len(reply.thinking)} chars"
                        if reply.thinking
                        else ""
                    )
                    nudge_msg = EMPTY_CONTINUE_NUDGE if tool_turns > 0 else EMPTY_FIRST_NUDGE
                    if empty_nudges_used < MAX_EMPTY_NUDGES:
                        empty_nudges_used += 1
                        if self._audit is not None:
                            self._audit.write(
                                "empty_response_nudge",
                                summary=f"attempt {empty_nudges_used}{thinking_hint}",
                            )
                        self._record(tool_result(nudge_msg))
                        continue
                    self._ui.show_status("(empty response)")
                    if self._audit is not None:
                        self._audit.write(
                            "empty_response",
                            summary=f"nudge budget exhausted{thinking_hint}",
                        )
                return reply

            tool_turns += 1
            if tool_turns > self._settings.runtime.max_tool_turns:
                self._ui.show_status("Tool budget for this turn is exhausted; wrapping up.")
                self._record(
                    tool_result(
                        "Tool budget exhausted for this turn. Answer now in plain text "
                        "with what you already know; do not call more tools."
                    )
                )
                tools = []
                continue

            # Capture completion BEFORE this batch runs so we can tell whether
            # the model's own update_plan transitioned the plan to completed in
            # THIS batch (an explicit completion) versus a plan that was already
            # completed coming in. Never inferred from prose.
            plan_completed_before = (
                self.plan_manager.active is not None
                and self.plan_manager.active.status == "completed"
            )

            for call in reply.tool_calls:
                self._ui.show_tool_call(call.name, call.arguments)
                outcome = executor.execute(call)
                # Feed side-effecting tool outcomes to the plan completion guard.
                # Key off the SPEC's side_effect (not the result's): a failed or
                # denied result carries SideEffect.NONE, which would hide exactly
                # the failures the guard exists to catch. Plan tools are
                # SideEffect.NONE, so they never pollute the counters.
                spec = self._registry.get(call.name)
                if (
                    spec is not None
                    and spec.side_effect is not SideEffect.NONE
                    and outcome.result is not None
                ):
                    self.plan_manager.note_side_effect(outcome.result.success)
                if outcome.malformed:
                    consecutive_malformed += 1
                    if consecutive_malformed >= MAX_CONSECUTIVE_MALFORMED:
                        self._ui.show_status(
                            "Repeated malformed tool calls; stopping tool use for this turn."
                        )
                        self._record(
                            tool_result(
                                f"{outcome.model_text}\nRepeated malformed tool calls. "
                                "Answer now in plain text without calling tools."
                            )
                        )
                        tools = []
                        break
                    self._record(
                        tool_result(f"{outcome.model_text}\nRetry once with a corrected call.")
                    )
                    continue
                consecutive_malformed = 0
                if outcome.result is not None:
                    self._ui.show_tool_result(
                        call.name, outcome.result.success, outcome.result.summary
                    )
                    diff = outcome.result.metadata.get("diff", "")
                    if outcome.result.success and diff:
                        self.recent_diffs.append(diff)
                self._record(tool_result(outcome.model_text))
                self._track_repeated_failure(call.name, outcome)

            # Drain images staged by view_image during THIS batch. Placed after
            # the for-loop so every exit path of the batch (normal completion
            # and the malformed-twice break) flows through here and cannot skip
            # clearing — a stale stage must not attach to a later message.
            if self._staged_tool_images:
                refs = tuple(self._staged_tool_images)
                self._staged_tool_images.clear()
                names = ", ".join(ref.path for ref in refs)
                self._record(
                    user(
                        f"[harness: image attached from view_image: {names}]",
                        images=refs,
                    )
                )

            # Suppress the redundant end-of-plan re-summary. When the model's own
            # update_plan(completed) transitioned the plan to completed in THIS
            # batch AND the same reply already carries a substantive summary, the
            # streamed prose IS the single summary — re-invoking the model on the
            # planner's end-of-plan summary prompt only duplicates
            # it. Completion is always explicit (the plan went through the normal
            # _update handler, so on_step_change/UI re-render and bookkeeping have
            # already run); we skip only the extra model round-trip, never the
            # completion itself, and never infer completion from prose. A short or
            # empty completing reply does NOT suppress, so the "summarize" prompt
            # still fires and elicits the single summary.
            active = self.plan_manager.active
            if (
                active is not None
                and active.status == "completed"
                and not plan_completed_before
                and len(reply.content.strip()) >= MIN_SUMMARY_CHARS
            ):
                if self._audit is not None:
                    self._audit.write("plan_summary_suppressed", summary=reply.content.strip())
                return reply

    def _track_repeated_failure(self, name: str, outcome: ExecutionOutcome) -> None:
        """Same safe recovery failing twice triggers the roadblock protocol (§11.6)."""
        if outcome.result is None or outcome.result.success:
            self._last_failure_signature = None
            return
        signature = f"{name}:{outcome.result.summary}"
        if signature == self._last_failure_signature:
            self._record(
                tool_result(
                    "The same action has now failed twice with the same result. Stop "
                    "this approach. Follow the roadblock protocol: record the blocker "
                    "with update_plan(blocker=...), then propose a revised plan or ask "
                    "the user one short, specific question."
                )
            )
            self._last_failure_signature = None
        else:
            self._last_failure_signature = signature
