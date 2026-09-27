"""Scope turn hook (SPEC §3): inject the scope rules every turn and refuse clear out-of-scope asks.

``before_model`` always returns the rendered scope rules. On a user turn that
:func:`relay.delegator.scope.detect_out_of_scope` flags, it adds a pointed refusal note and
records ``{"refused": true, "refusal_reason": ...}`` in that user turn's ``turns.metadata``.
Nothing here may break the turn: a failed DB write is logged and the notes are still returned.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import literal, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import TurnContext
from relay.delegator.demo.events import emit
from relay.delegator.scope import (
    REFUSAL_PHRASE,
    REFUSED_KEY,
    OutOfScope,
    detect_out_of_scope,
    render_scope_prompt,
)
from relay.store.models import Turn

log = logging.getLogger(__name__)
REFUSAL_WRITE_TIMEOUT_S = 0.5


def refusal_note(hit: OutOfScope) -> str:
    return (
        f"SCOPE: the user's latest request is out of scope ({hit.category.value}: "
        f'"{hit.matched}"). Refuse it in one short spoken sentence that starts with exactly '
        f'"{REFUSAL_PHRASE}". Do not call propose_commitment or dispatch_task, do not offer a '
        "workaround such as drafting it for them. Then continue the conversation."
    )


async def record_refusal(
    db: async_sessionmaker[AsyncSession], turn_id: uuid.UUID, reason: str
) -> None:
    """Merge ``{"refused": true, "refusal_reason": reason}`` into ``turns.metadata``."""
    payload: dict[str, Any] = {"refused": True, "refusal_reason": reason}
    async with db() as session, session.begin():
        await session.execute(
            update(Turn)
            .where(Turn.id == turn_id)
            .values(meta=Turn.meta.op("||")(literal(payload, type_=JSONB)))
        )


class ScopeHook:
    """``TurnHook`` enforcing the v1 scope boundary; construct with no arguments."""

    safety_critical = True

    def system_prefix(self, settings: Settings) -> str:
        """The scope rules are static, so they live in the cached prompt prefix."""
        return render_scope_prompt(settings)

    async def before_model(self, ctx: TurnContext) -> list[str]:
        notes: list[str] = []
        if not ctx.messages or ctx.messages[-1].get("role") != "user":
            return notes  # tool-result follow-up: the user said nothing new
        hit = detect_out_of_scope(ctx.user_text)
        if hit is None:
            ctx.state.extra.pop(REFUSED_KEY, None)
            return notes
        ctx.state.extra[REFUSED_KEY] = hit.category.value
        log.info("scope: refusing out-of-scope request (%s)", hit.reason)
        notes.append(refusal_note(hit))
        emit(
            ctx.state.session_id,
            "scope_refusal",
            {
                "turn_id": str(ctx.user_turn_id) if ctx.user_turn_id is not None else None,
                "utterance": ctx.user_text,
                "category": hit.category.value,
                "reason": (
                    f"Relay v1 only thinks and delegates research; it does not act on "
                    f"{hit.category.value} ({hit.matched!r})."
                ),
            },
        )
        if ctx.user_turn_id is not None:
            try:
                async with asyncio.timeout(REFUSAL_WRITE_TIMEOUT_S):
                    await record_refusal(ctx.db, ctx.user_turn_id, hit.reason)
            except TimeoutError:
                log.warning("scope: recording refusal on turn %s timed out", ctx.user_turn_id)
            except Exception:
                log.exception("scope: recording refusal on turn %s failed", ctx.user_turn_id)
        return notes

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None
