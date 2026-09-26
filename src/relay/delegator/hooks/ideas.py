"""Keep ``ideas.summary`` fresh so ``recall`` can answer "where did we land on X?".

*Whether* a summary is due is decided in ``after_response``: every ``every_n_user_turns`` user
turns, and whenever the focused idea actually changes from one idea to another (the idea being
left is then due too, so its final turns are not lost). The first focus of a session is not a
switch; it only starts the turn count.

*When* it runs: only once the session has been idle for ``idle_s``. Local Ollama typically
serves one request at a time, so a summary running during a live turn delays that turn's first
token by the whole summary. Each ``after_response`` (re)arms a per-session idle timer and each
``before_model`` cancels it, including a summary already in flight (it stays due and is retried
at the next pause). ``drain()`` flushes everything still due, immediately and bounded.
Failures are logged, never raised.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import TurnContext
from relay.delegator.llm.base import ChatModel
from relay.delegator.llm.factory import build_model
from relay.store import ideas_repo

log = logging.getLogger(__name__)

SUMMARY_IDEA_KEY = "ideas.summary_idea"  # idea id at the last regeneration
SUMMARY_TURN_KEY = "ideas.summary_turn"  # user_turn_index at the last regeneration
TITLE_KEY = "ideas.title"  # (idea id, title) cache for the before_model note
SUMMARY_TIMEOUT_S = 60.0
MAX_SUMMARY_CHARS = 1200

SYSTEM_PROMPT = (
    "You maintain the running summary of one idea a user is developing by talking it through. "
    "Write 2 to 4 plain sentences: what the idea is, what has been decided, and what is still "
    "open. No lists, no markdown, no preamble."
)


def build_prompt(title: str, previous: str | None, transcript: list[tuple[str, str]]) -> str:
    lines = [f"Idea: {title}"]
    if previous:
        lines.append(f"Previous summary: {previous}")
    lines.append("Recent conversation:")
    lines.extend(f"{role}: {' '.join(text.split())}" for role, text in transcript)
    lines.append("Updated summary:")
    return "\n".join(lines)


@dataclass
class _Due:
    """Summaries owed for one session, plus what is needed to run them without a turn."""

    db: async_sessionmaker[AsyncSession]
    settings: Settings
    ideas: list[uuid.UUID] = field(default_factory=list)


class IdeaSummaryHook:
    """``TurnHook``; construct with no arguments (``model`` is injectable for tests)."""

    def __init__(
        self,
        every_n_user_turns: int = 6,
        *,
        model: ChatModel | None = None,
        turns_window: int = 20,
        idle_s: float = 20.0,
        drain_timeout_s: float = SUMMARY_TIMEOUT_S,
    ) -> None:
        self._every = max(1, every_n_user_turns)
        self._model = model
        self._window = turns_window
        self._idle_s = idle_s
        self._drain_timeout_s = drain_timeout_s
        self._due: dict[uuid.UUID, _Due] = {}
        self._timers: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def before_model(self, ctx: TurnContext) -> list[str]:
        # The conversation is active again: nothing may compete with this turn for the model.
        self._cancel_timer(ctx.state.session_id)
        idea_id = ctx.state.current_idea_id
        if idea_id is None or not ctx.messages or ctx.messages[-1].get("role") != "user":
            return []
        cached: tuple[uuid.UUID, str] | None = ctx.state.extra.get(TITLE_KEY)
        if cached is None or cached[0] != idea_id:
            titles = await ideas_repo.get_idea_titles(ctx.db, [idea_id])
            if idea_id not in titles:
                return []
            cached = (idea_id, titles[idea_id])
            ctx.state.extra[TITLE_KEY] = cached
        # No id here: showing the id invites the model to "confirm" it via focus_idea.
        return [
            f'Current idea (already recorded): "{cached[1]}". Do not call focus_idea unless '
            "the user moves to a different idea."
        ]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        self._mark_due(ctx)
        sid = ctx.state.session_id
        if sid in self._due:
            self._cancel_timer(sid)
            task = asyncio.create_task(self._run_when_idle(sid), name=f"idea-summary:{sid}")
            self._timers[sid] = task
            task.add_done_callback(lambda t: self._forget_timer(sid, t))

    async def drain(self) -> None:
        """Cancel idle timers and run every due summary now (shutdown, tests); bounded."""
        timers = list(self._timers.values())
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        try:
            async with asyncio.timeout(self._drain_timeout_s):
                for sid in list(self._due):
                    await self._run_due(sid)
        except TimeoutError:
            log.warning("idea summaries still due at drain timeout: %d sessions", len(self._due))

    def _mark_due(self, ctx: TurnContext) -> None:
        idea_id = ctx.state.current_idea_id
        if idea_id is None:
            return
        extra = ctx.state.extra
        last_idea: uuid.UUID | None = extra.get(SUMMARY_IDEA_KEY)
        last_turn: int = extra.get(SUMMARY_TURN_KEY, 0)
        if last_idea is None:
            # First focus in this session is not a switch: start counting turns from here.
            extra[SUMMARY_IDEA_KEY] = idea_id
            extra[SUMMARY_TURN_KEY] = ctx.state.user_turn_index
            return
        switched = idea_id != last_idea
        if not switched and ctx.state.user_turn_index - last_turn < self._every:
            return
        extra[SUMMARY_IDEA_KEY] = idea_id
        extra[SUMMARY_TURN_KEY] = ctx.state.user_turn_index
        due = self._due.setdefault(ctx.state.session_id, _Due(ctx.db, ctx.settings))
        for target in [idea_id, last_idea] if switched else [idea_id]:
            if target not in due.ideas:
                due.ideas.append(target)

    def _cancel_timer(self, sid: uuid.UUID) -> None:
        task = self._timers.pop(sid, None)
        if task is not None:
            task.cancel()

    def _forget_timer(self, sid: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._timers.get(sid) is task:
            del self._timers[sid]

    async def _run_when_idle(self, sid: uuid.UUID) -> None:
        await asyncio.sleep(self._idle_s)
        await self._run_due(sid)

    async def _run_due(self, sid: uuid.UUID) -> None:
        """Summarise every idea due for ``sid``; an idea stays due only if cancelled mid-run."""
        due = self._due.get(sid)
        while due is not None and due.ideas:
            idea_id = due.ideas[0]
            try:
                async with asyncio.timeout(SUMMARY_TIMEOUT_S):
                    await self._regenerate_one(due.db, due.settings, idea_id)
            except Exception:
                log.warning("idea summary regeneration failed for %s", idea_id, exc_info=True)
            if idea_id in due.ideas:
                due.ideas.remove(idea_id)
        if due is not None and not due.ideas:
            self._due.pop(sid, None)

    async def _regenerate_one(
        self, db: async_sessionmaker[AsyncSession], settings: Settings, idea_id: uuid.UUID
    ) -> None:
        turns = await ideas_repo.recent_turns(db, idea_id, self._window)
        if not turns:
            return
        digest = await ideas_repo.get_idea_digest(db, idea_id)
        if digest is None:
            return
        if self._model is None:
            self._model = build_model(settings.summary_model, settings)
        prompt = build_prompt(digest.title, digest.summary, [(t.role, t.text) for t in turns])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        parts: list[str] = []
        async for delta in self._model.stream(messages, None, temperature=0.2, max_tokens=300):
            if delta.content:
                parts.append(delta.content)
        summary = " ".join("".join(parts).split())[:MAX_SUMMARY_CHARS]
        if summary:
            await ideas_repo.set_summary(db, idea_id, summary)
