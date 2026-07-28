"""Plan model, PLAN.md artifacts, and plan tools (design section 11).

The plan artifact under .shellpilot/tasks/<task-id>/PLAN.md is the reference of
record for a task: it survives /compact and crashes, and the runtime injects a
compact plan state into the model context on every planned step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.persistence.json_store import atomic_write_json, atomic_write_text
from shellpilot.persistence.paths import project_state_dir
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ALL_PROFILES, ToolContext, ToolResult, ToolSpec

STEP_STATUSES = ("pending", "active", "completed", "skipped")
PLAN_STATUSES = ("proposed", "active", "blocked", "completed", "cancelled")

STATE_VERSION = 1


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:max_words]
    return "-".join(words) or "task"


@dataclass
class PlanStep:
    title: str
    status: str = "pending"
    note: str = ""


@dataclass
class TaskPlan:
    task_id: str
    goal: str
    user_intent: str
    workspace: Path
    profile: str
    steps: list[PlanStep]
    assumptions: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    status: str = "proposed"
    created: str = field(default_factory=_now_iso)
    updated: str = field(default_factory=_now_iso)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    revisions: list[str] = field(default_factory=list)
    progress_log: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskPlan:
        steps = [
            PlanStep(
                title=str(s.get("title", "")),
                status=str(s.get("status", "pending")),
                note=str(s.get("note", "")),
            )
            for s in data.get("steps", [])
            if isinstance(s, dict)
        ]
        return cls(
            task_id=str(data["task_id"]),
            goal=str(data["goal"]),
            user_intent=str(data["user_intent"]),
            workspace=Path(str(data["workspace"])),
            profile=str(data["profile"]),
            steps=steps,
            assumptions=[str(x) for x in data.get("assumptions", [])],
            verification=[str(x) for x in data.get("verification", [])],
            status=str(data.get("status", "proposed")),
            created=str(data.get("created", _now_iso())),
            updated=str(data.get("updated", _now_iso())),
            decisions=[str(x) for x in data.get("decisions", [])],
            open_questions=[str(x) for x in data.get("open_questions", [])],
            blockers=[str(x) for x in data.get("blockers", [])],
            revisions=[str(x) for x in data.get("revisions", [])],
            progress_log=[str(x) for x in data.get("progress_log", [])],
        )


def load_plan(workspace: Path, task_id: str) -> TaskPlan | None:
    """Load plan from state.json sidecar; returns None on any error (self-healing)."""
    sidecar = project_state_dir(workspace) / "tasks" / task_id / "state.json"
    try:
        raw = sidecar.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("state_version") != STATE_VERSION:
        return None
    try:
        return TaskPlan.from_dict(data)
    except (KeyError, ValueError, TypeError):
        return None


def _section(title: str, items: list[str], empty: str = "- Pending.") -> str:
    body = "\n".join(f"- {item}" for item in items) if items else empty
    return f"## {title}\n\n{body}\n"


def render_plan_markdown(plan: TaskPlan) -> str:
    steps = "\n".join(
        f"- [{'x' if step.status == 'completed' else ' '}] {step.title}"
        + (f" ({step.status})" if step.status in ("active", "skipped") else "")
        + (f" — {step.note}" if step.note else "")
        for step in plan.steps
    )
    parts = [
        f"# Task Plan: {plan.goal}\n",
        f"Status: {plan.status}",
        f"Task ID: {plan.task_id}",
        f"Workspace: {plan.workspace}",
        f"Profile: {plan.profile}",
        f"Created: {plan.created}",
        f"Updated: {plan.updated}\n",
        f"## Goal\n\n{plan.goal}\n",
        f"## User Intent\n\n{plan.user_intent}\n",
        _section("Assumptions", plan.assumptions, "- None recorded."),
        f"## Plan\n\n{steps}\n",
        _section("Verification", plan.verification, "- None recorded."),
        _section("Decisions", plan.decisions),
        _section("Open Questions", plan.open_questions),
        _section("Blockers", plan.blockers, "- None."),
        _section("Revisions", plan.revisions, "- None."),
        _section("Progress Log", plan.progress_log, "- Created initial plan."),
    ]
    return "\n".join(parts)


def compact_plan_state(plan: TaskPlan) -> str:
    """Small plan-state block injected into the model context (section 11.3)."""
    steps = "; ".join(
        f"{index}.{step.title} [{step.status}]" for index, step in enumerate(plan.steps, 1)
    )
    block = (
        f"## Active task plan ({plan.status})\n"
        f"Task: {plan.task_id}\nGoal: {plan.goal}\nSteps: {steps}"
    )
    if plan.blockers:
        block += f"\nBlockers: {plan.blockers[-1]}"
    return block


class PlanManager:
    """Owns the active plan and its artifact on disk."""

    def __init__(self, workspace: Path, profile: str, *, max_plan_steps: int = 10) -> None:
        self._workspace = workspace
        self._profile = profile
        self.max_plan_steps = max_plan_steps
        self.active: TaskPlan | None = None
        self.pending_revision: str | None = None
        # Transient completion-guard state (runtime-only; never persisted to
        # PLAN.md/TaskPlan, mirroring _last_failure_signature). Tracks the
        # side-effecting tool outcomes seen against the currently active step so
        # that a step whose last side-effecting action failed cannot be marked
        # completed until something succeeds (or a blocker is recorded).
        self._step_se_failures: int = 0
        self._step_se_successes: int = 0
        self._guard_active_index: int | None = None
        self.on_change: Callable[[TaskPlan | None], None] | None = None

    def set_workspace(self, workspace: Path) -> None:
        """New tasks use the new boundary; an active plan keeps its artifact path."""
        self._workspace = workspace

    def set_profile(self, profile: str) -> None:
        """New tasks stamp *profile*; an active plan keeps its recorded profile."""
        self._profile = profile

    def artifact_path(self, plan: TaskPlan) -> Path:
        # Pinned to the plan's own workspace (set at create, persisted, restored),
        # not the mutable self._workspace — so a mid-plan /cwd keeps PLAN.md where
        # it was written instead of orphaning it under the new boundary (§11.3).
        return project_state_dir(plan.workspace) / "tasks" / plan.task_id / "PLAN.md"

    def _write(self, plan: TaskPlan) -> None:
        plan.updated = _now_iso()
        # Write sidecar first (crash-tolerant: sidecar before pointer)
        sidecar_path = self.artifact_path(plan).parent / "state.json"
        payload: dict[str, Any] = {
            "state_version": STATE_VERSION,
            "task_id": plan.task_id,
            "goal": plan.goal,
            "user_intent": plan.user_intent,
            "workspace": str(plan.workspace),
            "profile": plan.profile,
            "steps": [{"title": s.title, "status": s.status, "note": s.note} for s in plan.steps],
            "assumptions": plan.assumptions,
            "verification": plan.verification,
            "status": plan.status,
            "created": plan.created,
            "updated": plan.updated,
            "decisions": plan.decisions,
            "open_questions": plan.open_questions,
            "blockers": plan.blockers,
            "revisions": plan.revisions,
            "progress_log": plan.progress_log,
        }
        atomic_write_json(sidecar_path, payload)
        # Write PLAN.md
        atomic_write_text(self.artifact_path(plan), render_plan_markdown(plan))
        # Fire on_change AFTER both files written
        if self.on_change is not None:
            self.on_change(self.active)

    def create(
        self,
        *,
        goal: str,
        user_intent: str,
        steps: list[str],
        assumptions: list[str],
        verification: list[str],
    ) -> TaskPlan:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        plan = TaskPlan(
            task_id=f"{stamp}-{slugify(goal)}",
            goal=goal,
            user_intent=user_intent,
            workspace=self._workspace,
            profile=self._profile,
            steps=[PlanStep(title=title) for title in steps],
            assumptions=assumptions,
            verification=verification,
        )
        plan.progress_log.append(f"{_now_iso()}: Created initial plan.")
        self.active = plan
        self._write(plan)
        return plan

    def approve(self) -> None:
        assert self.active is not None
        self.active.status = "active"
        if self.active.steps:
            self.active.steps[0].status = "active"
        self.active.progress_log.append(f"{_now_iso()}: Plan approved by user.")
        self._write(self.active)

    def cancel(self) -> None:
        self.pending_revision = None
        if self.active is None:
            if self.on_change is not None:
                self.on_change(None)
            return
        self.active.status = "cancelled"
        self.active.progress_log.append(f"{_now_iso()}: Plan cancelled.")
        self._write(self.active)  # fires on_change(active) where active.status=="cancelled"
        self.active = None

    def restore(self, plan: TaskPlan) -> None:
        """Rehydrate a plan from a prior session without writing files or firing on_change."""
        self.active = plan
        # Reset transient completion-guard counters
        self._step_se_failures = 0
        self._step_se_successes = 0
        self._guard_active_index = None

    def revise(
        self,
        *,
        feedback: str,
        goal: str,
        steps: list[str],
        assumptions: list[str],
        verification: list[str],
    ) -> None:
        """Update the existing active plan in place with revised content.

        Records a ``revised: <feedback>`` progress-log entry and rewrites
        PLAN.md.  The task_id and directory are preserved — no new task is
        created.
        """
        assert self.active is not None
        self.active.goal = goal
        self.active.steps = [PlanStep(title=title) for title in steps]
        self.active.assumptions = assumptions
        self.active.verification = verification
        self.active.status = "proposed"
        self.active.progress_log.append(f"{_now_iso()}: revised: {feedback}")
        self.pending_revision = None
        self._write(self.active)

    def update_step(self, index: int, status: str, note: str = "") -> str:
        assert self.active is not None
        if not 1 <= index <= len(self.active.steps):
            return f"no step {index}; plan has {len(self.active.steps)} steps"
        step = self.active.steps[index - 1]
        step.status = status
        if note:
            step.note = note
        self.active.progress_log.append(f"{_now_iso()}: Step {index} -> {status}. {note}".rstrip())
        if status == "completed":
            nxt = next((s for s in self.active.steps if s.status == "pending"), None)
            if nxt is not None:
                nxt.status = "active"
        # Finalize after ANY status change: a skipped final step must complete the
        # plan too, otherwise it stays active forever (keeps injecting, the
        # end-of-plan summary never fires, the session pointer is never cleaned).
        if self.active.steps and all(
            s.status in ("completed", "skipped") for s in self.active.steps
        ):
            self.active.status = "completed"
        self._write(self.active)
        return ""

    def _live_active_index(self) -> int | None:
        """1-based index of the currently active step, or None if none/no plan."""
        if self.active is None:
            return None
        return next(
            (i for i, step in enumerate(self.active.steps, start=1) if step.status == "active"),
            None,
        )

    def note_side_effect(self, success: bool) -> None:
        """Record one side-effecting tool outcome against the active step.

        When the active step changes (advanced/replanned) the per-step counters
        reset so a prior step's failure never leaks into the next step's guard.
        """
        active_index = self._live_active_index()
        if active_index is None:
            self._step_se_failures = 0
            self._step_se_successes = 0
            self._guard_active_index = None
            return
        if active_index != self._guard_active_index:
            self._step_se_failures = 0
            self._step_se_successes = 0
            self._guard_active_index = active_index
        if success:
            self._step_se_successes += 1
        else:
            self._step_se_failures += 1

    def completion_blocked(self, index: int) -> bool:
        """True when completing ``index`` must be refused by the guard.

        Only the active step is ever guarded, and only when its last
        side-effecting action failed and nothing has succeeded since.
        """
        active_index = self._live_active_index()
        if active_index is None or active_index != self._guard_active_index:
            # Reconcile a stale guard index the same way note_side_effect does.
            self._step_se_failures = 0
            self._step_se_successes = 0
            self._guard_active_index = active_index
        if active_index is None or index != active_index:
            return False
        return self._step_se_failures > 0 and self._step_se_successes == 0

    def record_blocker(self, text: str) -> None:
        assert self.active is not None
        self.active.blockers.append(f"{_now_iso()}: {text}")
        self.active.status = "blocked"
        self._write(self.active)

    def record_revision(self, text: str) -> None:
        assert self.active is not None
        self.active.revisions.append(f"{_now_iso()}: {text}")
        if self.active.status == "blocked":
            self.active.status = "active"
        self._write(self.active)

    def log(self, text: str) -> None:
        if self.active is None:
            return
        self.active.progress_log.append(f"{_now_iso()}: {text}")
        self._write(self.active)


PlanApprovalAsker = Callable[["TaskPlan", str], tuple[str, str]]
UserIntentGetter = Callable[[], str]
PlanProgressShower = Callable[["TaskPlan"], None]


def make_plan_tools(
    manager: PlanManager,
    ask_plan_approval: PlanApprovalAsker,
    get_user_intent: UserIntentGetter,
    on_step_change: PlanProgressShower | None = None,
    max_plan_steps: int | None = None,
) -> list[ToolSpec]:
    """Plan tools close over the manager and the UI approval flow.

    When *max_plan_steps* is omitted, the live ``manager.max_plan_steps`` value
    is used so ``/config`` changes take effect without rebuilding the tools.
    """

    def _limit() -> int:
        return manager.max_plan_steps if max_plan_steps is None else max_plan_steps

    def _propose(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        goal = str(arguments["goal"]).strip()
        steps = [str(step) for step in arguments["steps"] if str(step).strip()]
        if not goal or not steps:
            return ToolResult(
                success=False, summary="plan needs a goal and at least one step", content=""
            )

        limit = _limit()
        if len(steps) > limit:
            n = len(steps)
            return ToolResult(
                success=False,
                summary=f"plan has {n} steps; max is {limit}",
                content=(
                    f"plan has {n} steps; max is {limit} — consolidate related "
                    "steps and propose again"
                ),
            )

        pending_feedback = manager.pending_revision
        if pending_feedback is not None and manager.active is not None:
            # A revision was requested: update the existing task in place.
            manager.revise(
                feedback=pending_feedback,
                goal=goal,
                steps=steps,
                assumptions=[str(item) for item in arguments.get("assumptions", [])],
                verification=[str(item) for item in arguments.get("verification", [])],
            )
            plan = manager.active
        elif (
            manager.active is not None
            and manager.active.status in ("proposed", "active")
            and manager.active.goal == goal
            and [step.title for step in manager.active.steps] == steps
        ):
            # Idempotent duplicate: the model re-emitted an identical propose_plan
            # for the already proposed/active plan (a known double-emit). Do NOT
            # cancel-and-recreate (that would re-prompt for approval of the same
            # plan); send the model back to executing the current step. Equality
            # compares the incoming normalized goal/steps against the stored form
            # (manager.create stores goal as-is and each step verbatim as
            # PlanStep.title), so a byte-identical re-emit matches while any real
            # goal/step change falls through to the recreate branch below.
            # NOTE: a deliberate identical "restart" re-propose is swallowed —
            # far rarer than the duplicate-emit this prevents.
            return ToolResult(
                success=True,
                summary="plan already active; continue executing",
                content=(
                    "This plan is already active — do not re-propose it. "
                    "Continue executing the current step now, in this same turn."
                ),
            )
        else:
            # No pending revision — cancel any stale active plan and create fresh.
            revising = manager.active is not None and manager.active.status in (
                "active",
                "blocked",
                "proposed",
            )
            if revising and manager.active is not None:
                manager.record_revision(f"Replaced by revised plan for goal: {goal}")
                manager.cancel()
            plan = manager.create(
                goal=goal,
                user_intent=get_user_intent(),
                steps=steps,
                assumptions=[str(item) for item in arguments.get("assumptions", [])],
                verification=[str(item) for item in arguments.get("verification", [])],
            )

        path = manager.artifact_path(plan)
        choice, revision_text = ask_plan_approval(plan, str(path))
        if choice == "y":
            manager.approve()
            first = plan.steps[0].title if plan.steps else "the task"
            return ToolResult(
                success=True,
                summary=f"plan approved ({plan.task_id})",
                content=(
                    f"Plan approved and saved to {path}. The user has already approved — never ask "
                    f"again in prose. Continue this same turn: call the tool for step 1 ({first}) "
                    "now, then update_plan(step=1, status='completed'), and keep executing steps "
                    "until the plan is complete or blocked."
                ),
            )
        if choice == "e":
            manager.pending_revision = revision_text
            return ToolResult(
                success=True,
                summary="user requested plan changes",
                content=(
                    f"The user wants changes to the plan: {revision_text}\n"
                    f"Call propose_plan again with a revised plan that addresses this feedback. "
                    f"Your next propose_plan call will UPDATE the existing task "
                    f"({plan.task_id}) in place — do not start a new task."
                ),
            )
        manager.cancel()
        return ToolResult(
            success=True,
            summary="plan rejected",
            content=(
                "The user rejected the plan. Do not execute it. "
                "Ask what they would like to do instead."
            ),
        )

    def _update(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if manager.active is None:
            return ToolResult(
                success=False,
                summary="no active plan",
                content="There is no active plan. Use propose_plan first.",
            )
        messages: list[str] = []
        step = arguments.get("step")
        status = arguments.get("status", "")
        if step is not None and status:
            if status not in STEP_STATUSES:
                return ToolResult(
                    success=False,
                    summary=f"invalid status {status!r}",
                    content=(
                        f"status must be one of {', '.join(STEP_STATUSES)}. To record a "
                        "blocker, use the blocker argument: "
                        'update_plan(blocker="<evidence>"), not a status value.'
                    ),
                )
            if status == "completed" and manager.completion_blocked(int(step)):
                return ToolResult(
                    success=False,
                    summary=f"step {step} not completed: last action failed",
                    content=(
                        f"Step {step}'s last side-effecting action failed (e.g. edit rejected) "
                        "and nothing has succeeded since. Apply the change successfully before "
                        "marking this step completed, or record a blocker with "
                        'update_plan(blocker="<evidence>").'
                    ),
                )
            error = manager.update_step(int(step), str(status), str(arguments.get("note", "")))
            if error:
                return ToolResult(success=False, summary=error, content=error)
            messages.append(f"step {step} -> {status}")
            if on_step_change is not None and manager.active is not None:
                on_step_change(manager.active)
        elif arguments.get("note"):
            manager.log(str(arguments["note"]))
            messages.append("noted")
        if arguments.get("blocker"):
            manager.record_blocker(str(arguments["blocker"]))
            messages.append("blocker recorded")
            return ToolResult(
                success=True,
                summary="; ".join(messages),
                content=(
                    "Blocker recorded; the plan is now blocked. Follow the roadblock "
                    "protocol: capture the failing evidence, re-check the stale "
                    "assumption with the narrowest safe read, then either call "
                    "propose_plan with a revised plan or ask the user one short, "
                    "specific question."
                ),
            )
        plan = manager.active
        done = plan.status == "completed"
        return ToolResult(
            success=True,
            summary="; ".join(messages) or "plan unchanged",
            content=(
                (
                    "All steps complete. Give the user your final summary now — "
                    "match its length to the task: a brief confirmation for simple "
                    "work, a fuller summary when there are substantive findings "
                    "worth reporting."
                )
                if done
                else (
                    f"Plan updated. Current state:\n{compact_plan_state(plan)}\n"
                    "Continue with the next step now, in this same turn."
                )
            ),
        )

    propose = ToolSpec(
        definition=ToolDefinition(
            name="propose_plan",
            description=(
                "Propose a step-by-step plan for a multi-step task and ask the user to "
                "approve it. Required for tasks needing 3 or more distinct steps. "
                "Do not use it for a single command or a single file edit — do those directly."
            ),
            parameters={
                "goal": {"type": "string", "description": "One-sentence task goal."},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered step titles, each a concrete action.",
                },
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Assumptions the plan relies on.",
                },
                "verification": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "How the result will be verified.",
                },
            },
            required=("goal", "steps"),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_propose,
    )
    update = ToolSpec(
        definition=ToolDefinition(
            name="update_plan",
            description=(
                "Update the active plan: mark a step's status, add a progress note, "
                "or record a blocker when something invalidates the plan."
            ),
            parameters={
                "step": {"type": "integer", "description": "1-based step number."},
                "status": {
                    "type": "string",
                    "enum": list(STEP_STATUSES),
                    "description": "New step status: pending, active, completed, or skipped.",
                },
                "note": {"type": "string", "description": "Short progress note."},
                "blocker": {
                    "type": "string",
                    "description": "Describe what is blocking the plan, with evidence.",
                },
            },
            required=(),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_update,
    )
    return [propose, update]
