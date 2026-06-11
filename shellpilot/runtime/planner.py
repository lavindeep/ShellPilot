"""Plan model, PLAN.md artifacts, and plan tools (design section 11).

The plan artifact under .shellpilot/tasks/<task-id>/PLAN.md is the reference of
record for a task: it survives /compact and crashes, and the runtime injects a
compact plan state into the model context on every planned step.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shellpilot.llm.messages import ToolDefinition
from shellpilot.persistence.json_store import atomic_write_text
from shellpilot.persistence.paths import project_state_dir
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import ToolContext, ToolResult, ToolSpec
from shellpilot.tools.filesystem import ALL_PROFILES

STEP_STATUSES = ("pending", "active", "completed", "skipped")
PLAN_STATUSES = ("proposed", "active", "blocked", "completed", "cancelled")


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


def render_plan_terminal(plan: TaskPlan) -> str:
    lines = [f"Goal: {plan.goal}", ""]
    if plan.assumptions:
        lines.append("Assumptions:")
        lines.extend(f"- {item}" for item in plan.assumptions)
        lines.append("")
    lines.append("Plan:")
    for index, step in enumerate(plan.steps, start=1):
        marker = {"completed": "✓", "active": "→", "skipped": "·"}.get(step.status, " ")
        lines.append(f"{index}. [{marker}] {step.title}")
    if plan.verification:
        lines.append("")
        lines.append("Verification:")
        lines.extend(f"- {item}" for item in plan.verification)
    return "\n".join(lines)


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

    def __init__(self, workspace: Path, profile: str) -> None:
        self._workspace = workspace
        self._profile = profile
        self.active: TaskPlan | None = None

    def set_workspace(self, workspace: Path) -> None:
        """New tasks use the new boundary; an active plan keeps its artifact path."""
        self._workspace = workspace

    def artifact_path(self, plan: TaskPlan) -> Path:
        return project_state_dir(self._workspace) / "tasks" / plan.task_id / "PLAN.md"

    def _write(self, plan: TaskPlan) -> None:
        plan.updated = _now_iso()
        atomic_write_text(self.artifact_path(plan), render_plan_markdown(plan))

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
        if self.active is None:
            return
        self.active.status = "cancelled"
        self.active.progress_log.append(f"{_now_iso()}: Plan cancelled.")
        self._write(self.active)
        self.active = None

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
            elif all(s.status in ("completed", "skipped") for s in self.active.steps):
                self.active.status = "completed"
        self._write(self.active)
        return ""

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
) -> list[ToolSpec]:
    """Plan tools close over the manager and the UI approval flow."""

    def _propose(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        goal = str(arguments["goal"]).strip()
        steps = [str(step) for step in arguments["steps"] if str(step).strip()]
        if not goal or not steps:
            return ToolResult(
                success=False, summary="plan needs a goal and at least one step", content=""
            )
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
            manager.record_revision(f"User requested changes: {revision_text}")
            return ToolResult(
                success=True,
                summary="user requested plan changes",
                content=(
                    f"The user wants changes to the plan: {revision_text}\n"
                    "Call propose_plan again with a revised plan that addresses this."
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
                    content=f"status must be one of {', '.join(STEP_STATUSES)}",
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
                "All steps complete. Summarize the task outcome for the user."
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
