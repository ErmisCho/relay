"""``get_status`` internal tool: speakable task progress for the voice agent."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from dbos import DBOSClient

from relay.config import Settings
from relay.delegator.contracts import ToolContext, ToolResult
from relay.executor.common import get_dbos_client_async, one_sentence
from relay.executor.status import TaskStatus, get_status

log = logging.getLogger(__name__)


def describe(task: TaskStatus) -> str:
    """One short spoken sentence for ``task``."""
    what = f"The {task.kind} task on {task.goal!r}"
    if task.status == "succeeded":
        tail = one_sentence(task.artifact_summary, "")
        return f"{what} is done." + (f" {tail}" if tail else "")
    if task.status == "failed":
        return f"{what} failed: {one_sentence(task.error, 'unknown error')}"
    if task.status == "running":
        return f"{what} is in progress."
    if task.workflow_status is None:
        return f"{what} is queued, waiting for the executor to pick it up."
    return f"{what} is queued and will start shortly."


async def _default_client(settings: Settings) -> DBOSClient | None:
    try:
        return await get_dbos_client_async(settings)
    except Exception:
        log.warning("DBOS client unavailable; reporting database status only", exc_info=True)
        return None


class GetStatusTool:
    """``InternalTool`` reporting the status of a task or of the current idea's tasks."""

    name = "get_status"
    description = (
        "Check progress of delegated work. Pass task_id, or idea_id; with neither, reports "
        "on the idea currently being discussed."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "UUID of a task."},
            "idea_id": {"type": "string", "description": "UUID of an idea."},
        },
        "additionalProperties": False,
    }

    def __init__(
        self, client_factory: Callable[[Settings], Awaitable[DBOSClient | None]] = _default_client
    ) -> None:
        self._client_factory = client_factory

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            task_id = uuid.UUID(args["task_id"]) if args.get("task_id") else None
            idea_id = uuid.UUID(args["idea_id"]) if args.get("idea_id") else None
        except (ValueError, TypeError):
            return ToolResult("That id is not valid.", rejected=True)
        if task_id is None and idea_id is None:
            idea_id = ctx.state.current_idea_id
        if task_id is None and idea_id is None:
            return ToolResult("There is no current idea, so there is no task to check.")
        tasks = await get_status(
            ctx.db,
            task_id=task_id,
            idea_id=idea_id,
            client=await self._client_factory(ctx.settings),
        )
        if not tasks:
            return ToolResult("No work has been dispatched for that yet.")
        text = describe(tasks[0])
        if len(tasks) > 1:
            text += f" There are {len(tasks) - 1} earlier tasks for this idea."
        return ToolResult(text)
