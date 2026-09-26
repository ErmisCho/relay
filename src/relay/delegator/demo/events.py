"""In-process, per-session event feed for the demo website (TASK-42).

Delegator code paths call :func:`emit` at each decision point (turns, scope refusal, current
idea, ready gate, proposal, assent, dispatch). ``emit`` is synchronous and O(1): it appends to a
bounded per-session history and ``put_nowait``s into each subscriber's bounded queue, dropping
that subscriber's oldest event when it is full. Nothing on the voice turn awaits a consumer.

The bus is a module-level singleton (:data:`BUS`) because the emitters (hooks, tools, detached
reservations) share no handle to the app. It stays disabled (``emit`` returns at once) unless
the demo is enabled (``DEMO_PASSCODE`` set). History lives in memory only: a Delegator restart
loses replay, which is acceptable for a demo.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Events kept per session for replay (``Last-Event-ID`` / ``after``).
HISTORY_PER_SESSION = 2000
#: Sessions whose history is kept; the least recently active unwatched one is evicted first.
MAX_SESSIONS = 64
#: Per-subscriber queue bound; a slow consumer loses its oldest events, never blocks emit.
SUBSCRIBER_QUEUE = 256


def iso_ms(ts: datetime) -> str:
    """``2026-09-26T16:42:04.120Z`` (UTC, millisecond precision)."""
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class _Channel:
    seq: int = 0
    history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=HISTORY_PER_SESSION)
    )
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)


class EventBus:
    def __init__(self) -> None:
        self.enabled = False
        self._channels: OrderedDict[str, _Channel] = OrderedDict()
        #: task_id -> (session_id, last emitted status); read by the task poller.
        self.watched_tasks: dict[uuid.UUID, tuple[str, str]] = {}

    def _channel(self, session_id: str) -> _Channel:
        chan = self._channels.get(session_id)
        if chan is not None:
            self._channels.move_to_end(session_id)
            return chan
        chan = self._channels[session_id] = _Channel()
        while len(self._channels) > MAX_SESSIONS:
            victim = next((k for k, c in self._channels.items() if not c.subscribers), None)
            if victim is None or victim == session_id:
                break
            del self._channels[victim]
        return chan

    def emit(self, session_id: uuid.UUID | str, type_: str, data: dict[str, Any]) -> None:
        if not self.enabled:
            return
        sid = str(session_id)
        chan = self._channel(sid)
        chan.seq += 1
        event = {
            "seq": chan.seq,
            "session_id": sid,
            "ts": iso_ms(datetime.now(UTC)),
            "type": type_,
            "data": data,
        }
        chan.history.append(event)
        for queue in chan.subscribers:
            if queue.full():
                queue.get_nowait()  # drop-oldest for a slow consumer
            queue.put_nowait(event)

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._channel(session_id).subscribers.add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        chan = self._channels.get(session_id)
        if chan is not None:
            chan.subscribers.discard(queue)

    def history(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        chan = self._channels.get(session_id)
        return [e for e in chan.history if e["seq"] > after] if chan else []


BUS = EventBus()


def emit(session_id: uuid.UUID | str, type_: str, data: dict[str, Any]) -> None:
    """Fire-and-forget: record ``type_`` for ``session_id`` on the demo feed (no-op when off)."""
    BUS.emit(session_id, type_, data)


def emit_dispatch(
    session_id: uuid.UUID,
    *,
    proposal_id: uuid.UUID,
    commitment_id: uuid.UUID,
    task_id: uuid.UUID,
    workflow_id: str,
    kind: str,
    idea_id: uuid.UUID | None,
) -> None:
    """``dispatch`` then ``task_status(queued)``, and hand the task to the status poller."""
    if not BUS.enabled:
        return
    emit(
        session_id,
        "dispatch",
        {
            "proposal_id": str(proposal_id),
            "commitment_id": str(commitment_id),
            "task_id": str(task_id),
            "workflow_id": workflow_id,
            "kind": kind,
            "idea_id": str(idea_id) if idea_id is not None else None,
        },
    )
    emit(session_id, "task_status", {"task_id": str(task_id), "status": "queued", "error": None})
    BUS.watched_tasks[task_id] = (str(session_id), "queued")
