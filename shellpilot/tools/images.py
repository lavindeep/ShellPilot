"""view_image tool: open a workspace image for vision-capable sessions (task B10).

Ollama vision chat templates only render images attached to USER messages;
images on tool-role messages are ignored.  So this tool cannot return the
image in its result.  Instead the handler validates and loads the image, then
STAGES the ref via a callback; the conversation runtime drains the stage after
the current tool-call batch and appends a synthetic user-role message carrying
the image with a harness marker, so the model sees it on the next turn and
provenance stays clear in history and transcripts.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from shellpilot.cli.attachments import AttachmentError, load_image
from shellpilot.llm.messages import ImageRef, ToolDefinition
from shellpilot.policy.risk import RiskLevel, SideEffect
from shellpilot.tools.base import (
    ALL_PROFILES,
    ToolContext,
    ToolResult,
    ToolSpec,
    WorkspaceBoundaryError,
    resolve_in_workspace,
)


def make_view_image_tool(
    stage: Callable[[ImageRef], None],
    is_vision: Callable[[], bool],
) -> ToolSpec:
    """Build the view_image ToolSpec.

    *stage* receives the loaded ImageRef for next-message delivery.
    *is_vision* is consulted at call time so /model switches are respected.
    """

    def _view(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        raw_path = str(arguments["path"])
        try:
            path = resolve_in_workspace(context.workspace, raw_path)
        except WorkspaceBoundaryError as exc:
            return ToolResult(success=False, summary=str(exc), content=str(exc))
        if not is_vision():
            message = (
                "the active model does not advertise vision; ask the user to switch with /model use"
            )
            return ToolResult(success=False, summary=message, content=message)
        try:
            ref = load_image(Path(path))
        except AttachmentError as exc:
            return ToolResult(success=False, summary=str(exc), content=str(exc))
        stage(ref)
        return ToolResult(
            success=True,
            summary=f"viewed {raw_path}",
            content=(
                "Image loaded. It is attached to the next message you receive — "
                "describe or use it there."
            ),
        )

    return ToolSpec(
        definition=ToolDefinition(
            name="view_image",
            description=(
                "Open an image file from the workspace so you can see it. "
                "The image arrives ATTACHED TO THE NEXT MESSAGE you receive, not "
                "in this tool result — describe or use it there. "
                "Only works in vision-capable sessions. "
                "Supports png, jpg, jpeg, gif, webp up to 10 MB."
            ),
            parameters={
                "path": {
                    "type": "string",
                    "description": "Image path, relative to the workspace.",
                },
            },
            required=("path",),
        ),
        side_effect=SideEffect.NONE,
        default_risk=RiskLevel.LOW,
        allowed_profiles=ALL_PROFILES,
        handler=_view,
    )
