"""The commitment protocol (SPEC §6): score, propose, assent, dispatch.

Invariant: the gate may only ever *propose*. ``dispatch_task`` is authorised here, server-side,
from facts the model cannot forge:

1. a pending proposal exists in this session's (in-memory) state,
2. the request that proposed it streamed to completion, and its server-generated read-back ends
   the assistant message ElevenLabs recorded right before the current user turn (a barge-in
   truncates that message),
3. the assent classifier labelled the current user turn -- the one right after the proposal
   -- ``affirmative`` (checked in :meth:`CommitmentHook.before_model`, before the model runs),
4. the scope guard still passes for the proposal.

Even then ``dispatch_task`` only *reserves*. ElevenLabs re-sends a user turn after a false
barge-in -- extended ("Yes" -> "Yes, but what about Europe?") or revised by ASR, up to ~2.8 s
later -- so nothing is written until ``DISPATCH_GRACE_S`` has passed since the LATEST user
request of the session. EVERY user request that arrives while a dispatch is reserved (re-send,
revision or an apparently new turn) is classified against the reservation's read-back: a clear
``affirmative`` restarts the grace, anything else cancels it. A finished answer stream is not a
commit trigger (it ends when the model is done, not when the user is). The commit +
``start_task`` then run in a detached task that a client disconnect cannot cancel; at shutdown
reservations still waiting are CANCELLED (nothing could cancel them any more), and
:func:`relay.delegator.commitment.reconcile.start_orphaned_commitments` repairs a crash between
commit and ``start_task`` at startup.

Every other path is rejected and writes nothing. A proposal lives for exactly one user turn and
is dispatched at most once; a restart drops it. When in doubt: no dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.adapters.openai_compat import content_text
from relay.delegator.commitment.classify import (
    ASSENT_TIMEOUT_S,
    AssentLabel,
    AssentResult,
    DirectiveLabel,
    DirectiveResult,
    classify_assent,
    classify_directive,
    score_ready,
)
from relay.delegator.commitment.readback import (
    ARTIFACT_NOUN,
    ARTIFACT_PHRASE,
    build_readback,
    clean_field,
    normalise,
    previous_assistant_text,
    readback_delivered,
)
from relay.delegator.contracts import (
    PendingProposal,
    SessionState,
    ToolContext,
    ToolResult,
    TurnContext,
)
from relay.delegator.demo.events import emit, emit_dispatch
from relay.delegator.llm.base import ChatModel
from relay.delegator.llm.factory import build_model
from relay.delegator.scope import ArtifactKind, scope_guard, validate_commitment_args
from relay.delegator.service import COMPLETED_KEY
from relay.executor.dispatch import StartedTask, start_task
from relay.store.models import Commitment, Idea, RouterDecision

log = logging.getLogger(__name__)

#: ``SessionState.extra`` key holding the :class:`AssentRecord` of the current user turn.
ASSENT_KEY = "commitment.assent"
#: ``SessionState.extra`` key: set of proposal ids already dispatched in this session.
DISPATCHED_KEY = "commitment.dispatched"
#: ``SessionState.extra`` key: the latest :class:`Reservation` of this session.
RESERVATION_KEY = "commitment.reservation"
#: ``SessionState.extra`` key: ``{proposal_id: bool}``, did the proposing request finish streaming.
PROPOSAL_STREAM_KEY = "commitment.proposal_stream"
#: ``SessionState.extra`` key: a note owed to the model on the next user turn.
NEXT_NOTE_KEY = "commitment.next_note"
#: ``SessionState.extra`` key: the current user turn as a :class:`DirectRequest`.
REQUEST_KEY = "commitment.request"

#: A reserved dispatch commits this long after the session's latest user request (the longest
#: live ASR revision gap measured was 2.79 s).
DISPATCH_GRACE_S = 3.5
#: The ready scores of a session are batch-computed once it has been idle this long: on the
#: single Ollama slot any scoring call evicts the conversation's prompt cache.
READY_IDLE_S = 20.0
#: ``SessionState.extra`` key: ``time.monotonic()`` of the session's latest user request.
LAST_USER_REQUEST_AT_KEY = "commitment.last_user_request_at"
READY_CONTEXT_MESSAGES = 8
DRAIN_TIMEOUT_S = 30.0

# Detached commit tasks, strongly referenced so they outlive the request that started them.
_BACKGROUND: set[asyncio.Task[Any]] = set()
# Reservations whose task has not finished, so shutdown can cancel the ones still waiting.
_LIVE_RESERVATIONS: set[Reservation] = set()


def _spawn(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


@dataclass(frozen=True)
class AssentRecord:
    """Outcome of the assent check for one user turn; the only thing that authorises dispatch."""

    proposal_id: uuid.UUID
    user_turn_index: int
    user_turn_id: uuid.UUID | None
    utterance: str
    label: AssentLabel
    source: str
    delivered: bool
    classified_at: datetime


@dataclass(frozen=True)
class DirectRequest:
    """A fresh user request (not an answer to a read-back) that may start work directly.

    Set by :meth:`CommitmentHook.before_model`; ``classify`` (bound to the hook's assent model)
    labels it against a proposed goal. Only a turn with this record can skip the read-back.
    """

    user_turn_index: int
    user_turn_id: uuid.UUID | None
    utterance: str
    previous: str
    classify: Callable[[str], Awaitable[DirectiveResult]]


# --- per-turn notes ----------------------------------------------------------------------

NOTE_AFFIRMATIVE = (
    "Commitment protocol: the user just clearly agreed to the plan you read back. Call "
    "dispatch_task now (no arguments). Say the work has started only if dispatch_task succeeds."
)
NOTE_NOT_AFFIRMATIVE: dict[AssentLabel, str] = {
    AssentLabel.HEDGE: (
        "Commitment protocol: the user did NOT clearly agree to the plan you read back, so it "
        "was cancelled. Do not call dispatch_task and do not say the work has started. Respond "
        "to what they said and keep talking; propose again only once they are clearly ready."
    ),
    AssentLabel.NEGATIVE: (
        "Commitment protocol: the user declined or paused the plan you read back, so it was "
        "cancelled. Do not call dispatch_task and do not say the work has started. Acknowledge "
        "briefly and continue the conversation."
    ),
    AssentLabel.NEW_INFORMATION: (
        "Commitment protocol: the user added or changed something instead of agreeing, so the "
        "plan you read back was cancelled. Do not call dispatch_task and do not say the work "
        "has started. Take the new information into account; if the idea is then clearly "
        "ready, call propose_commitment again with the updated plan."
    ),
}
NOTE_ALREADY_STARTING = (
    "Commitment protocol: the work the user agreed to is already starting. Do not call "
    "dispatch_task or propose_commitment for it again; just confirm briefly that it is starting."
)
NOTE_CANCELLED: dict[AssentLabel, str] = {
    label: (
        "Commitment protocol: the user's full reply was not a clear yes, so the work you were "
        "about to start was CANCELLED and did NOT start. Tell the user in one short sentence "
        "that you have not started it, then respond to what they said. Do not call "
        "dispatch_task."
    )
    for label in AssentLabel
}
NOTE_CANCELLED_SHUTDOWN = (
    "Commitment protocol: the work you said was starting did NOT start: the service was "
    "restarting before it could begin. Tell the user briefly and offer to set it up again."
)
NOTE_FAILED = (
    "Commitment protocol: the work you said was starting could NOT be started because of an "
    "internal error. Tell the user briefly that it did not start, and offer to try again later."
)
NOTE_INTERRUPTED = (
    "Commitment protocol: your confirmation question was interrupted before the user heard all "
    "of it, so it was cancelled. Do not call dispatch_task and do not say the work has started. "
    "Respond to what the user said; propose again later if appropriate."
)

SYSTEM_PREFIX = """\
Commitment protocol (starting background work):
- You can never start work by saying so. Work starts only through the tools below.
- When the idea feels ready (the goal and what to leave out are clear and the user wants it \
done), call propose_commitment with the goal, what is explicitly excluded and the artifact \
kind. Do not just say you will do it.
- Say nothing before calling propose_commitment. When the user plainly told you to do the \
work, it starts right away and tells them so itself. When they were only asking for ideas, \
suggestions or your opinion, it starts nothing and you answer them in your own words. Rarely \
it speaks a confirmation question instead; then wait for the user's answer.
- Call dispatch_task only when a system note tells you the user agreed. The server decides; if \
it rejects the call, do not retry and do not pretend the work started.
- Never claim work has started, is running or will be done unless dispatch_task succeeded.
- If readiness is borderline or anything is unclear, ask one short clarifying question instead \
of proposing."""


# --- tools -------------------------------------------------------------------------------


def _rejected(why: str) -> ToolResult:
    return ToolResult(
        content=(
            f"REJECTED: {why} This call started nothing new. Do not say that new work has "
            "started. Continue the conversation."
        ),
        rejected=True,
    )


NOT_YET_RESULT = (
    "NOT started and nothing was said to the user: they are still exploring (asking for ideas, "
    "suggestions, feedback or your opinion), not asking you to start work. Answer them now in "
    "your own words. If a task would help, describe it in one short sentence and ask whether "
    "they want you to start it; call propose_commitment again only once they ask for it."
)


ALREADY_STARTED_RESULT = ToolResult(
    content=(
        "Already started: the work the user agreed to is already starting or running. Do not "
        "call dispatch_task again. Confirm briefly that it is underway."
    )
)


def _active_reservation(state: SessionState) -> Reservation | None:
    """A reservation that is waiting or being written, or was committed in this user turn."""
    res = state.extra.get(RESERVATION_KEY)
    if not isinstance(res, Reservation):
        return None
    if res.status in ("reserved", "committing"):
        return res
    if res.status == "committed" and res.user_turn_index == state.user_turn_index:
        return res
    return None


class ProposeCommitmentTool:
    """``propose_commitment``: stores a pending proposal with a server-generated read-back."""

    name = "propose_commitment"
    description = (
        "Hand the current idea to a background worker. Call this instead of saying you will do "
        "the work; say nothing before calling it. If the user plainly told you to do it, the "
        "server starts it at once; otherwise it speaks a confirmation question and nothing "
        "starts until the user explicitly agrees."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "The user's request as an imperative phrase, in their own words, e.g. "
                    "'research proximity bike locks' or 'check my hardware system settings'."
                ),
            },
            "scope_excludes": {
                "type": "string",
                "description": "What is explicitly left out, as a noun phrase, e.g. 'pricing'.",
            },
            "artifact_kind": {
                "type": "string",
                "enum": [a.value for a in ArtifactKind],
                "description": (
                    "Terminal artifact: 'document' for looking into, checking or inspecting "
                    "something and writing a report; 'pull_request' for making or changing "
                    "code in a project."
                ),
            },
        },
        "required": ["goal", "scope_excludes", "artifact_kind"],
        "additionalProperties": False,
    }

    def __init__(
        self, start: StartTask | None = None, *, grace_s: float = DISPATCH_GRACE_S
    ) -> None:
        self._start: StartTask = start or start_task
        self._grace_s = grace_s

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if (refused := scope_guard(args, ctx)) is not None:
            return refused
        state = ctx.state
        if _active_reservation(state) is not None:
            return _rejected(
                "the work the user just agreed to is already starting; do not propose it again."
            )
        record = state.extra.get(ASSENT_KEY)
        if (
            isinstance(record, AssentRecord)
            and state.pending_proposal is not None
            and record.proposal_id == state.pending_proposal.proposal_id
            and record.label is AssentLabel.AFFIRMATIVE
        ):
            return _rejected(
                "the user already agreed to the current proposal. Call dispatch_task now "
                "instead of proposing again."
            )
        decision = validate_commitment_args(args, ctx.settings)
        assert decision.kind is not None and decision.artifact_kind is not None
        goal = clean_field(args.get("goal"))
        excludes = clean_field(args.get("scope_excludes"))
        if not goal or not excludes:
            return _rejected(
                "a proposal needs a non-empty goal and an explicit scope exclusion. Ask the user "
                "what should be left out, then call propose_commitment again."
            )
        readback = build_readback(goal, excludes, decision.artifact_kind)
        # A newer proposal replaces the older one; any assent recorded for it is void.
        state.pending_proposal = PendingProposal(
            goal=goal,
            scope_excludes=excludes,
            artifact_kind=decision.artifact_kind.value,
            kind=decision.kind.value,
            readback_text=readback,
            idea_id=state.current_idea_id,
            proposed_at_user_turn=state.user_turn_index,
        )
        state.extra.pop(ASSENT_KEY, None)
        p = state.pending_proposal
        judged = await self._directive(ctx, p)
        if judged is not None and judged[1].label is DirectiveLabel.EXPLORING:
            # Still exploring (asking for ideas, feedback, opinions): no read-back, the model
            # answers in its own words. Only a failed check falls back to the read-back.
            state.pending_proposal = None
            log.info("commitment: proposal skipped, user still exploring: %r", judged[0].utterance)
            return ToolResult(content=NOT_YET_RESULT)
        directive = judged
        if directive is not None:
            p.readback_text = (
                f"Started directly on the user's instruction: I'll {goal}, leaving out "
                f"{excludes}, and I'll leave it as {ARTIFACT_PHRASE[decision.artifact_kind]}."
            )
        emit(
            state.session_id,
            "proposal",
            {
                "proposal_id": str(p.proposal_id),
                "idea_id": str(p.idea_id) if p.idea_id is not None else None,
                "goal": p.goal,
                "scope_excludes": p.scope_excludes,
                "artifact_kind": p.artifact_kind,
                "readback": p.readback_text,
                "direct": directive is not None,
            },
        )
        log.info(
            "commitment: proposal %s session=%s turn=%d direct=%s",
            p.proposal_id,
            state.session_id,
            state.user_turn_index,
            directive is not None,
        )
        if directive is not None:
            return self._start_now(ctx, p, *directive)
        # Spoken by the server, not the model: a model round after the tool call repeated
        # earlier sentences around the read-back and appended a report announcement to it.
        return ToolResult(
            content=(
                f'Proposal recorded, NOT started. The confirmation question "{readback}" was '
                "spoken to the user; wait for their answer."
            ),
            speak=readback,
        )

    async def _directive(
        self, ctx: ToolContext, pending: PendingProposal
    ) -> tuple[DirectRequest, DirectiveResult] | None:
        """The request and its judged label (directive, or a confident exploring).

        Judged server-side from the user's words, never from the model's arguments. None (no
        fresh request, direct starts off, or the check failed) keeps the read-back.
        """
        state = ctx.state
        request = state.extra.get(REQUEST_KEY)
        if (
            not ctx.settings.direct_dispatch
            or not isinstance(request, DirectRequest)
            or request.user_turn_index != state.user_turn_index
        ):
            return None
        result = await request.classify(pending.goal)
        log.info(
            "commitment: directive label=%s source=%s latency_ms=%d utterance=%r",
            result.label.value,
            result.source,
            result.latency_ms,
            request.utterance,
        )
        return None if result.source == "error" else (request, result)

    def _start_now(
        self,
        ctx: ToolContext,
        pending: PendingProposal,
        request: DirectRequest,
        result: DirectiveResult,
    ) -> ToolResult:
        """Reserve the proposal with the request itself as the recorded assent.

        The grace window still applies: a re-send that is no longer a directive cancels it.
        """
        state = ctx.state
        record = AssentRecord(
            proposal_id=pending.proposal_id,
            user_turn_index=state.user_turn_index,
            user_turn_id=request.user_turn_id or ctx.user_turn_id,
            utterance=request.utterance,
            label=AssentLabel.AFFIRMATIVE,
            source=f"directive:{result.source}",
            delivered=True,
            classified_at=datetime.now(UTC),
        )
        _assent(state, pending.proposal_id, record.user_turn_id, record.utterance, record.label)
        state.pending_proposal = None
        state.extra.setdefault(DISPATCHED_KEY, set()).add(pending.proposal_id)
        state.extra[RESERVATION_KEY] = Reservation(
            pending, record, state, ctx.db, self._start, self._grace_s, direct=True
        )
        noun = ARTIFACT_NOUN[ArtifactKind(pending.artifact_kind)]
        return ToolResult(
            content=(
                "Started: the user plainly asked for this, so it is starting without a "
                "confirmation question. Do not call dispatch_task or propose it again."
            ),
            speak=f"On it. I'll let you know when the {noun} is ready.",
        )


class StartTask(Protocol):
    def __call__(
        self,
        db: async_sessionmaker[AsyncSession],
        *,
        commitment_id: uuid.UUID,
        kind: str,
        session_id: uuid.UUID | None = None,
    ) -> Awaitable[StartedTask]: ...


def authorise(state: SessionState) -> tuple[PendingProposal, AssentRecord] | str:
    """The server-side dispatch decision: the proposal and its assent, or why not."""
    pending = state.pending_proposal
    if pending is None:
        return "there is no pending proposal the user has agreed to."
    if pending.proposal_id in state.extra.get(DISPATCHED_KEY, set()):
        return "this proposal was already dispatched."
    record = state.extra.get(ASSENT_KEY)
    if not isinstance(record, AssentRecord) or record.proposal_id != pending.proposal_id:
        return "the user has not answered the confirmation question yet. Wait for their reply."
    if (
        state.user_turn_index != pending.proposed_at_user_turn + 1
        or record.user_turn_index != state.user_turn_index
    ):
        return "the user's agreement must come in the turn right after the confirmation question."
    if not record.delivered:
        return "the confirmation question was not fully heard by the user."
    if record.label is not AssentLabel.AFFIRMATIVE:
        return "the user did not clearly agree."
    return pending, record


class DispatchTaskTool:
    """``dispatch_task``: authorised server-side only; reserves, then commits once settled."""

    name = "dispatch_task"
    description = (
        "Start the proposed work. Only call this when a system note says the user clearly "
        "agreed to your confirmation question. The server verifies this and rejects otherwise."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self, start: StartTask | None = None, *, grace_s: float = DISPATCH_GRACE_S
    ) -> None:
        self._start: StartTask = start or start_task
        self._grace_s = grace_s

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        state = ctx.state
        if state.pending_proposal is None and _active_reservation(state) is not None:
            return ALREADY_STARTED_RESULT  # a replayed / repeated call in the same user turn
        # The scope guard runs on the stored proposal: dispatch_task takes no arguments, and
        # whatever the model passes must not be able to change what was agreed to.
        pending = state.pending_proposal
        if pending is not None:
            scope_args = {"artifact_kind": pending.artifact_kind, "kind": pending.kind}
            if (refused := scope_guard(scope_args, ctx)) is not None:
                state.pending_proposal = None
                _dropped(state, pending.proposal_id, "scope_guard")
                return refused
        verdict = authorise(state)
        if isinstance(verdict, str):
            log.warning("commitment: dispatch rejected session=%s: %s", state.session_id, verdict)
            return _rejected(f"dispatch_task is not allowed: {verdict}")
        pending, record = verdict
        # Take-and-clear before anything else: at most one dispatch per proposal.
        state.pending_proposal = None
        state.extra.pop(ASSENT_KEY, None)
        state.extra.setdefault(DISPATCHED_KEY, set()).add(pending.proposal_id)
        state.extra[RESERVATION_KEY] = Reservation(
            pending, record, state, ctx.db, self._start, self._grace_s
        )
        noun = ARTIFACT_NOUN[ArtifactKind(pending.artifact_kind)]
        return ToolResult(
            content=(
                "Starting. Tell the user in one short sentence: "
                f'"Okay, starting on that now. I\'ll let you know when the {noun} is ready." '
                "Then carry on with the conversation."
            )
        )


ReservationStatus = Literal["reserved", "committing", "committed", "cancelled", "failed"]


class Reservation:
    """An authorised dispatch waiting for the user's turn to be truly over.

    Commits ``grace_s`` after the session's latest user request; :meth:`reaffirm` (a new request
    classified affirmative) restarts that clock and :meth:`cancel` stops it while still
    ``reserved``. The commit runs in a detached task, immune to request cancellation.
    """

    def __init__(
        self,
        proposal: PendingProposal,
        record: AssentRecord,
        state: SessionState,
        db: async_sessionmaker[AsyncSession],
        start: StartTask,
        grace_s: float,
        *,
        direct: bool = False,
    ) -> None:
        self.proposal = proposal
        # Started on a directive request rather than a spoken yes to a read-back.
        self.direct = direct
        self.record = record
        self.user_turn_index = record.user_turn_index
        self.status: ReservationStatus = "reserved"
        self.cancel_reason: str | None = None
        self.held = False
        self.commitment_id: uuid.UUID | None = None
        self.task_id: uuid.UUID | None = None
        self._state = state
        self._db = db
        self._start = start
        self._grace_s = grace_s
        self._wake = asyncio.Event()
        since = state.extra.get(LAST_USER_REQUEST_AT_KEY, time.monotonic())
        self._deadline = float(since) + grace_s
        _LIVE_RESERVATIONS.add(self)
        self.task = _spawn(self._run(), f"commitment-dispatch:{proposal.proposal_id}")
        self.task.add_done_callback(lambda _: _LIVE_RESERVATIONS.discard(self))

    def reaffirm(self, record: AssentRecord, at: float) -> None:
        """A newer request was a clear yes: keep it, with the grace measured from ``at``."""
        if self.status == "reserved":
            self.record = record
            self._deadline = at + self._grace_s
            self.held = False
            self._wake.set()

    def cancel(self, reason: str, *, drop_reason: str = "not_affirmative") -> bool:
        """Cancel if nothing was written yet; True when cancelled."""
        if self.status != "reserved":
            return False
        self.status = "cancelled"
        self.cancel_reason = reason
        self.held = False
        self._wake.set()
        _dropped(self._state, self.proposal.proposal_id, drop_reason)
        log.info("commitment: reservation %s cancelled: %s", self.proposal.proposal_id, reason)
        return True

    def hold(self) -> None:
        """Freeze the commit while a newer request is being classified (set synchronously,
        before any await, so the grace cannot expire mid-classification)."""
        if self.status == "reserved":
            self.held = True
            self._wake.set()

    async def _run(self) -> None:
        while self.status == "reserved":
            self._wake.clear()
            if self.held:
                await self._wake.wait()
                continue
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except TimeoutError:
                pass
        if self.status != "reserved":
            return
        self.status = "committing"
        state = self._state
        try:
            idea_id, self.commitment_id = await _record_commitment(
                self._db, state, self.proposal, self.record
            )
        except Exception:
            self.status = "failed"
            log.exception("commitment: recording commitment failed session=%s", state.session_id)
            state.extra[NEXT_NOTE_KEY] = NOTE_FAILED
            return
        self.status = "committed"
        state.current_idea_id = idea_id
        try:
            started = await self._start(
                self._db,
                commitment_id=self.commitment_id,
                kind=self.proposal.kind,
                session_id=state.session_id,
            )
        except Exception:
            # The commitment stands (the user agreed); startup reconciliation retries it.
            log.exception("commitment: start_task failed for commitment %s", self.commitment_id)
            return
        self.task_id = started.task_id
        emit_dispatch(
            state.session_id,
            proposal_id=self.proposal.proposal_id,
            commitment_id=self.commitment_id,
            task_id=started.task_id,
            workflow_id=started.workflow_id,
            kind=self.proposal.kind,
            idea_id=idea_id,
        )
        log.info(
            "commitment: dispatched commitment=%s task=%s session=%s assent=%r",
            self.commitment_id,
            started.task_id,
            state.session_id,
            self.record.utterance,
        )


def cancel_waiting_reservations(reason: str) -> list[Reservation]:
    """Cancel every reservation still waiting (shutdown); returns the ones cancelled.

    Each cancellation is logged at WARNING and leaves a note for the session's next turn (only
    reachable if the process keeps running; after a restart the log line is the record).
    """
    cancelled = [r for r in list(_LIVE_RESERVATIONS) if r.cancel(reason, drop_reason="expired")]
    for res in cancelled:
        res._state.extra[NEXT_NOTE_KEY] = NOTE_CANCELLED_SHUTDOWN
        log.warning(
            "commitment: NOT STARTED (%s) session=%s goal=%r assent=%r readback=%r",
            reason,
            res._state.session_id,
            res.proposal.goal,
            res.record.utterance,
            res.proposal.readback_text,
        )
    return cancelled


async def _record_commitment(
    db: async_sessionmaker[AsyncSession],
    state: SessionState,
    pending: PendingProposal,
    record: AssentRecord,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert the commitment and mark its idea committed, in one transaction."""
    async with db() as session, session.begin():
        idea_id = pending.idea_id or state.current_idea_id
        if idea_id is None or await session.get(Idea, idea_id) is None:
            idea = Idea(title=pending.goal, status="committed")
            session.add(idea)
            await session.flush()
            idea_id = idea.id
        else:
            await session.execute(
                update(Idea)
                .where(Idea.id == idea_id)
                .values(status="committed", updated_at=func.now())
            )
        commitment = Commitment(
            idea_id=idea_id,
            goal=pending.goal,
            scope_excludes=pending.scope_excludes,
            artifact_kind=pending.artifact_kind,
            readback_text=pending.readback_text,
            assent_utterance=record.utterance,
            assented_at=record.classified_at,
            # The user turn upserted this session row; the yes turn is NULL only if persisting
            # it failed (the dispatch still stands on the recorded utterance).
            session_id=state.session_id,
            assent_turn_id=record.user_turn_id,
        )
        session.add(commitment)
        await session.flush()
        return idea_id, commitment.id


# --- hook --------------------------------------------------------------------------------


@dataclass
class _Unscored:
    """A user turn waiting for its ready score, with what scoring it needs."""

    turn_id: uuid.UUID
    transcript: list[tuple[str, str]]
    db: async_sessionmaker[AsyncSession]
    settings: Settings
    session_id: uuid.UUID | None = None
    idea_id: uuid.UUID | None = None


class CommitmentHook:
    """``TurnHook`` + ``SystemPrefixProvider``: delivery check, assent, expiry, ready score.

    Construct with no arguments; the models are injectable for tests. Register after
    ``ScopeHook``.
    """

    safety_critical = True

    def __init__(
        self,
        *,
        assent_model: ChatModel | None = None,
        ready_model: ChatModel | None = None,
        assent_timeout_s: float = ASSENT_TIMEOUT_S,
        score_ready: bool = True,
        ready_idle_s: float = READY_IDLE_S,
        drain_timeout_s: float = DRAIN_TIMEOUT_S,
    ) -> None:
        self._assent_model = assent_model
        self._ready_model = ready_model
        self._assent_timeout_s = assent_timeout_s
        self._score_ready = score_ready
        self._ready_idle_s = ready_idle_s
        self._drain_timeout_s = drain_timeout_s
        self._unscored: dict[uuid.UUID, list[_Unscored]] = {}
        self._timers: dict[uuid.UUID, asyncio.Task[None]] = {}
        self.skipped_scores = 0

    def system_prefix(self, settings: Settings) -> str:
        return SYSTEM_PREFIX

    def _model(self, which: str, settings: Settings) -> ChatModel | None:
        try:
            if which == "assent":
                if self._assent_model is None:
                    self._assent_model = build_model(settings.assent_model, settings)
                return self._assent_model
            if self._ready_model is None:
                self._ready_model = build_model(settings.ready_model, settings)
            return self._ready_model
        except Exception:
            log.exception("commitment: building the %s model failed", which)
            return None

    async def _classify(self, ctx: TurnContext, readback: str, utterance: str) -> AssentResult:
        result = await classify_assent(
            self._model("assent", ctx.settings), readback, utterance, timeout=self._assent_timeout_s
        )
        log.info(
            "commitment: assent label=%s source=%s latency_ms=%d utterance=%r",
            result.label.value,
            result.source,
            result.latency_ms,
            utterance,
        )
        return result

    def _offer_direct(self, ctx: TurnContext, utterance: str) -> None:
        """Mark this fresh request (not a reply to a read-back) as a direct-start candidate."""
        previous = previous_assistant_text(ctx.messages) or ""
        settings = ctx.settings

        async def classify(goal: str) -> DirectiveResult:
            model = self._model("assent", settings)
            return await classify_directive(
                model, previous, utterance, goal, timeout=self._assent_timeout_s
            )

        ctx.state.extra[REQUEST_KEY] = DirectRequest(
            ctx.state.user_turn_index, ctx.user_turn_id, utterance, previous, classify
        )

    async def _redirect(self, ctx: TurnContext, goal: str, utterance: str) -> AssentResult:
        """Directive check of a re-sent request, as an assent result (directive = yes)."""
        previous = previous_assistant_text(ctx.messages) or ""
        model = self._model("assent", ctx.settings)
        result = await classify_directive(
            model, previous, utterance, goal, timeout=self._assent_timeout_s
        )
        log.info(
            "commitment: re-sent directive label=%s source=%s utterance=%r",
            result.label.value,
            result.source,
            utterance,
        )
        label = (
            AssentLabel.AFFIRMATIVE
            if result.label is DirectiveLabel.DIRECTIVE
            else AssentLabel.NEW_INFORMATION
        )
        return AssentResult(label, f"directive:{result.source}", result.latency_ms)

    async def before_model(self, ctx: TurnContext) -> list[str]:
        # The conversation is active: no ready score may compete with it for the model.
        self._cancel_timer(ctx.state.session_id)
        if not ctx.messages or ctx.messages[-1].get("role") != "user":
            return []  # not a new user turn (e.g. a follow-up request): nothing to judge
        state = ctx.state
        now = time.monotonic()
        state.extra[LAST_USER_REQUEST_AT_KEY] = now
        notes: list[str] = []
        if (owed := state.extra.pop(NEXT_NOTE_KEY, None)) is not None:
            notes.append(owed)
        state.extra.pop(ASSENT_KEY, None)
        state.extra.pop(REQUEST_KEY, None)
        utterance = content_text(ctx.messages[-1].get("content"))

        res = state.extra.get(RESERVATION_KEY)
        if isinstance(res, Reservation) and res.status == "reserved":
            # ANY user request while a dispatch waits (re-send, ASR revision, new turn) is judged
            # against its read-back. Hold first, synchronously: the grace must not run out
            # while the classifier is still thinking.
            res.hold()
            try:
                if res.direct and state.user_turn_index == res.user_turn_index:
                    # A re-send/revision of the request that started it: still a directive?
                    result = await self._redirect(ctx, res.proposal.goal, utterance)
                else:
                    result = await self._classify(ctx, res.proposal.readback_text, utterance)
            except BaseException:  # raised or request cancelled: never leave a hold behind
                res.cancel("classification of a later request did not finish")
                raise
            _assent(state, res.proposal.proposal_id, ctx.user_turn_id, utterance, result.label)
            if res.status != "reserved":
                return [*notes, NOTE_CANCELLED[AssentLabel.HEDGE]]
            follow_up = _is_follow_up(ctx.messages, res, state)
            if result.label is AssentLabel.AFFIRMATIVE:
                res.reaffirm(_record(res.proposal, state, ctx, utterance, result), now)
                return [*notes, NOTE_ALREADY_STARTING]
            if follow_up and result.label is AssentLabel.HEDGE and result.source != "error":
                # A pleasantry after the spoken confirmation ("Thanks.") is not a retraction.
                res.reaffirm(res.record, now)
                return [*notes, NOTE_ALREADY_STARTING]
            reason = f"later request classified {result.label.value} ({result.source})"
            if res.cancel(f"{reason}: {utterance!r}"):
                return [*notes, NOTE_CANCELLED[result.label]]
        if _active_reservation(state) is not None:
            return [*notes, NOTE_ALREADY_STARTING]  # committing, or committed this turn

        pending = state.pending_proposal
        if pending is None:
            self._offer_direct(ctx, utterance)
            return notes
        if state.user_turn_index != pending.proposed_at_user_turn + 1:
            state.pending_proposal = None
            _dropped(state, pending.proposal_id, "expired")
            log.info("commitment: proposal %s expired", pending.proposal_id)
            self._offer_direct(ctx, utterance)
            return notes
        streamed = state.extra.get(PROPOSAL_STREAM_KEY, {}).get(pending.proposal_id) is True
        if not streamed or not readback_delivered(ctx.messages, pending.readback_text):
            state.pending_proposal = None
            _dropped(state, pending.proposal_id, "readback_interrupted")
            log.info("commitment: proposal %s read-back not delivered", pending.proposal_id)
            return [*notes, NOTE_INTERRUPTED]
        result = await self._classify(ctx, pending.readback_text, utterance)
        _assent(state, pending.proposal_id, ctx.user_turn_id, utterance, result.label)
        if result.label is not AssentLabel.AFFIRMATIVE:
            state.pending_proposal = None
            _dropped(state, pending.proposal_id, "not_affirmative")
            if result.label is not AssentLabel.NEGATIVE:
                # "Yes, and what I want is X, write it up" is a new instruction, not a no:
                # a re-proposal from it may start directly.
                self._offer_direct(ctx, utterance)
            return [*notes, NOTE_NOT_AFFIRMATIVE[result.label]]
        state.extra[ASSENT_KEY] = _record(pending, state, ctx, utterance, result)
        return [*notes, NOTE_AFFIRMATIVE]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        state = ctx.state
        completed = state.extra.get(COMPLETED_KEY, {}).get(state.user_turn_index) is True
        pending = state.pending_proposal
        if pending is not None and pending.proposed_at_user_turn == state.user_turn_index:
            # Precondition for assent: the read-back's stream was not cut off.
            state.extra.setdefault(PROPOSAL_STREAM_KEY, {})[pending.proposal_id] = completed
        if self._score_ready and ctx.user_turn_id is not None:
            self._queue_score(ctx)

    # --- ready score: hint + audit, batch-computed only when the session is idle ----------

    def _queue_score(self, ctx: TurnContext) -> None:
        assert ctx.user_turn_id is not None
        sid = ctx.state.session_id
        transcript = [
            (str(m.get("role")), content_text(m.get("content")))
            for m in ctx.messages
            if m.get("role") in ("user", "assistant")
        ][-READY_CONTEXT_MESSAGES:]
        queue = self._unscored.setdefault(sid, [])
        # A re-sent turn keeps its id: score only its latest version.
        queue[:] = [u for u in queue if u.turn_id != ctx.user_turn_id]
        queue.append(
            _Unscored(
                ctx.user_turn_id,
                transcript,
                ctx.db,
                ctx.settings,
                session_id=sid,
                idea_id=ctx.state.current_idea_id,
            )
        )
        self._cancel_timer(sid)
        task = asyncio.create_task(self._score_when_idle(sid), name=f"ready-score:{sid}")
        self._timers[sid] = task
        task.add_done_callback(lambda t: self._forget_timer(sid, t))

    def _forget_timer(self, sid: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._timers.get(sid) is task:
            del self._timers[sid]

    def _cancel_timer(self, sid: uuid.UUID) -> None:
        task = self._timers.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()
            self.skipped_scores += 1
            log.debug("commitment: ready scoring for session %s deferred (new request)", sid)

    async def _score_when_idle(self, sid: uuid.UUID) -> None:
        await asyncio.sleep(self._ready_idle_s)
        await self._score_all(sid)

    async def _score_all(self, sid: uuid.UUID) -> None:
        """Score the session's queued turns; a turn leaves the queue once attempted."""
        queue = self._unscored.get(sid, [])
        while queue:
            item = queue[0]
            await self._score_one(item)
            if queue and queue[0] is item:
                queue.pop(0)
        self._unscored.pop(sid, None)

    async def _score_one(self, item: _Unscored) -> None:
        try:
            model = self._model("ready", item.settings)
            if model is None:
                return
            started = time.perf_counter()
            ready = await score_ready(model, item.transcript)
            latency_ms = round((time.perf_counter() - started) * 1000)
            if ready is None:
                return
            async with item.db() as session, session.begin():
                session.add(
                    RouterDecision(
                        turn_id=item.turn_id,
                        backend="frontier",
                        ready=ready,
                        latency_ms=latency_ms,
                        # A reference score, not the turn's routing decision: the active row per
                        # turn belongs to ROUTER_ACTIVE (TASK-37), and both would collide.
                        is_active=False,
                    )
                )
            if item.session_id is not None:
                emit(
                    item.session_id,
                    "ready_gate",
                    {
                        "idea_id": str(item.idea_id) if item.idea_id is not None else None,
                        "verdict": ready,
                        "reason": None,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("commitment: ready score for turn %s failed", item.turn_id, exc_info=True)

    async def drain(self) -> None:
        """Shutdown: cancel waiting dispatches, score what is left (bounded), await commits."""
        cancel_waiting_reservations("shutdown before the grace window ended")
        for sid in list(self._timers):
            task = self._timers.pop(sid)
            task.cancel()
        try:
            async with asyncio.timeout(self._drain_timeout_s):
                for sid in list(self._unscored):
                    await self._score_all(sid)
                if _BACKGROUND:
                    await asyncio.wait(set(_BACKGROUND))
        except TimeoutError:
            missing = sum(len(q) for q in self._unscored.values())
            log.warning("commitment: drain timed out; %d ready scores missing", missing)
            for task in list(_BACKGROUND):
                task.cancel()


def _is_follow_up(messages: list[dict[str, Any]], res: Reservation, state: SessionState) -> bool:
    """True for a NEW user turn after the confirmation of the reservation was fully spoken.

    The history must read: the user's yes, then a completed assistant reply, then this message.
    Re-sends and revisions of the assent turn itself (no reply in between, or a reply that was
    cut off) are not follow-ups and keep the strict rule.
    """
    if state.user_turn_index <= res.user_turn_index or len(messages) < 3:
        return False
    reply, assent = messages[-2], messages[-3]
    if reply.get("role") != "assistant" or not content_text(reply.get("content")).strip():
        return False
    if assent.get("role") != "user":
        return False
    if normalise(content_text(assent.get("content"))) != normalise(res.record.utterance):
        return False
    return state.extra.get(COMPLETED_KEY, {}).get(res.user_turn_index) is True


def _record(
    proposal: PendingProposal,
    state: SessionState,
    ctx: TurnContext,
    utterance: str,
    result: AssentResult,
) -> AssentRecord:
    return AssentRecord(
        proposal_id=proposal.proposal_id,
        user_turn_index=state.user_turn_index,
        user_turn_id=ctx.user_turn_id,
        utterance=utterance,
        label=result.label,
        source=result.source,
        delivered=True,
        classified_at=datetime.now(UTC),
    )


# --- demo event feed (fire-and-forget; see relay.delegator.demo.events) -------------------


def _dropped(state: SessionState, proposal_id: uuid.UUID, reason: str) -> None:
    emit(state.session_id, "proposal_dropped", {"proposal_id": str(proposal_id), "reason": reason})


def _assent(
    state: SessionState,
    proposal_id: uuid.UUID,
    turn_id: uuid.UUID | None,
    utterance: str,
    label: AssentLabel,
) -> None:
    emit(
        state.session_id,
        "assent",
        {
            "proposal_id": str(proposal_id),
            "turn_id": str(turn_id) if turn_id is not None else None,
            "label": label.value,
            "utterance": utterance,
        },
    )
