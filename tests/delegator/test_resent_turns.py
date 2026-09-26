"""ElevenLabs re-sends a user turn after a (false) barge-in; it must stay ONE user turn."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import SessionStore, ToolRegistry, TurnContext
from relay.delegator.llm import ChatDelta
from relay.delegator.service import EMPTY_REPLY_TEXT, PREV_TEXT_KEY, RESENT_KEY
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Turn

from .conftest import ScriptedChatModel, make_settings, parse_sse, post, text


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


class Spy:
    def __init__(self) -> None:
        self.seen: list[tuple[int, bool, str | None, uuid.UUID | None]] = []

    async def before_model(self, ctx: TurnContext) -> list[str]:
        extra = ctx.state.extra
        self.seen.append(
            (
                ctx.state.user_turn_index,
                extra.get(RESENT_KEY),
                extra.get(PREV_TEXT_KEY),
                ctx.user_turn_id,
            )
        )
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None


async def _run(db: async_sessionmaker[AsyncSession], bodies: list[list[dict[str, Any]]]) -> Spy:
    spy = Spy()
    chat = ScriptedChatModel([text("ok") for _ in bodies])
    app = create_app(
        make_settings(),
        chat_model=chat,
        registry=ToolRegistry(),
        hooks=[spy],
        session_store=SessionStore(),
        sessionmaker=db,
        warm_dbos=False,
    )
    sid = str(uuid.uuid4())
    for messages in bodies:
        await post(
            app,
            {"messages": messages, "stream": True, "elevenlabs_extra_body": {"session_id": sid}},
        )
    return spy


SYS = {"role": "system", "content": "sys"}


async def _user_rows(db: async_sessionmaker[AsyncSession], tag: str) -> list[Turn]:
    async with db() as s:
        return list(
            await s.scalars(select(Turn).where(Turn.role == "user", Turn.text.contains(tag)))
        )


async def test_identical_resend_is_the_same_user_turn(db: async_sessionmaker[AsyncSession]) -> None:
    tag = uuid.uuid4().hex
    msgs = [SYS, {"role": "user", "content": f"I'm thinking {tag}."}]
    spy = await _run(db, [msgs, msgs])
    (first, second) = spy.seen
    assert first[:2] == (1, False)
    assert second[:3] == (1, True, f"I'm thinking {tag}.")
    assert second[3] == first[3]  # same persisted turn
    assert len(await _user_rows(db, tag)) == 1


async def test_extended_resend_updates_the_existing_user_row(
    db: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    short = [SYS, {"role": "user", "content": f"Who is {tag}, who is this"}]
    longer = [SYS, {"role": "user", "content": f"Who is {tag}, who is this responding? Like"}]
    spy = await _run(db, [short, longer])
    assert [s[:2] for s in spy.seen] == [(1, False), (1, True)]
    (row,) = await _user_rows(db, tag)
    assert row.text.endswith("responding? Like")
    assert row.meta.get("resent_from") == f"Who is {tag}, who is this"


async def test_a_new_turn_after_a_reply_is_not_a_resend(
    db: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    one = [SYS, {"role": "user", "content": f"Yeah {tag}."}]
    two = [
        *one,
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": f"Yeah {tag}."},
    ]
    different = [SYS, {"role": "user", "content": f"Something else {tag}"}]
    spy = await _run(db, [one, two])
    assert [s[:2] for s in spy.seen] == [(1, False), (2, False)]
    spy = await _run(db, [one, different])
    assert [s[:2] for s in spy.seen] == [(1, False), (2, False)]


# --- empty model output (seen live from gemma4) ------------------------------------------

EMPTY: list[ChatDelta] = [ChatDelta(finish_reason="stop")]


class WithFallback(ScriptedChatModel):
    def __init__(self, scripts: list[Any], fallback: ScriptedChatModel) -> None:
        super().__init__(scripts)
        self.fallback = fallback


async def _spoken(chat: Any, db: async_sessionmaker[AsyncSession]) -> str:
    app = create_app(
        make_settings(),
        chat_model=chat,
        registry=ToolRegistry(),
        hooks=[],
        session_store=SessionStore(),
        sessionmaker=db,
        warm_dbos=False,
    )
    resp = await post(
        app, {"messages": [SYS, {"role": "user", "content": "hello"}], "stream": True}
    )
    return "".join(c["choices"][0]["delta"].get("content") or "" for c in parse_sse(resp.content))


async def test_empty_output_is_retried_once_with_the_same_model(
    db: async_sessionmaker[AsyncSession],
) -> None:
    chat = ScriptedChatModel([EMPTY, text("Hi there.")])
    assert await _spoken(chat, db) == "Hi there."
    assert len(chat.calls) == 2


async def test_empty_output_then_uses_the_fallback_model(
    db: async_sessionmaker[AsyncSession],
) -> None:
    fallback = ScriptedChatModel([text("From the fallback.")])
    chat = WithFallback([EMPTY, EMPTY], fallback)
    assert await _spoken(chat, db) == "From the fallback."


async def test_still_empty_speaks_a_short_request_to_repeat(
    db: async_sessionmaker[AsyncSession],
) -> None:
    chat = ScriptedChatModel([EMPTY, EMPTY])
    assert await _spoken(chat, db) == EMPTY_REPLY_TEXT
