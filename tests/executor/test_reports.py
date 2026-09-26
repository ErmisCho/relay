"""Completion reports land at the next turn boundary and are confirmed only when spoken."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import SessionState, TurnContext
from relay.delegator.hooks.reports import ReportsHook
from relay.executor.common import one_sentence
from relay.executor.dispatch import start_task
from relay.store.models import PendingReport, Task
from tests.executor.conftest import Seed, Worker, task_row, wait_for
from tests.executor.fake_runners import summary_for

Db = async_sessionmaker[AsyncSession]
pytestmark = pytest.mark.usefixtures("no_pending_reports")


class Conversation:
    """Drives the hook like the Delegator: before_model, then after_response per turn."""

    def __init__(self, db: Db, session_id: uuid.UUID) -> None:
        self.db, self.hook = db, ReportsHook()
        self.state = SessionState(session_id=session_id)
        self.history: list[dict[str, Any]] = []

    def _ctx(self) -> TurnContext:
        user = str(self.history[-1]["content"])
        return TurnContext(
            state=self.state,
            db=self.db,
            settings=Settings(),
            messages=list(self.history),
            user_text=user,
        )

    async def begin(self, user: str) -> list[str]:
        self.history.append({"role": "user", "content": user})
        return await self.hook.before_model(self._ctx())

    async def end(self, generated: str, recorded: str | None = None) -> None:
        """``generated`` is what the model streamed; ``recorded`` what ElevenLabs kept."""
        await self.hook.after_response(self._ctx(), generated)
        content = generated if recorded is None else recorded
        self.history.append({"role": "assistant", "content": content})


def _report(engine: Engine, task_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as c:
        sql = text("SELECT * FROM pending_reports WHERE task_id = :t")
        return dict(c.execute(sql, {"t": task_id}).mappings().one())


SUMMARY = "Found three vineyards with autumn availability near Napa."


@pytest.fixture
def report(sync_engine: Engine, seed: Callable[[str], Seed]) -> tuple[Seed, uuid.UUID]:
    s = seed("vineyard venues")
    task_id = uuid.uuid4()
    with sync_engine.begin() as c:
        c.execute(
            insert(Task).values(
                id=task_id, commitment_id=s.commitment_id, kind="research", status="succeeded"
            )
        )
        c.execute(insert(PendingReport).values(idea_id=s.idea_id, task_id=task_id, summary=SUMMARY))
    return s, task_id


async def test_completion_announced_on_next_turn_not_mid_response(
    db: Db,
    seed: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: Worker,
) -> None:
    s = seed("SLOW garden venues")
    started = await start_task(
        db,
        commitment_id=s.commitment_id,
        kind="research",
        session_id=s.session_id,
        client=dbos_client,
    )
    conv = Conversation(db, s.session_id)
    assert await conv.begin("what about the budget?") == []
    # The task completes while turn 1's response is streaming ...
    (worker.flag_dir / f"release-{started.task_id}").touch()
    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] == "succeeded")
    await conv.end("Let's cap it at twenty thousand.")
    # ... but is only offered at the start of the next turn; the artifact exists regardless.
    assert _report(sync_engine, started.task_id)["offered_at"] is None
    with sync_engine.connect() as c:
        sql = text("SELECT url FROM artifacts WHERE task_id = :t")
        assert c.execute(sql, {"t": started.task_id}).scalar_one().endswith(".md")

    summary = one_sentence(summary_for("SLOW garden venues"), "")
    notes = await conv.begin("sounds good")
    assert len(notes) == 1 and f'"{summary}"' in notes[0]
    assert _report(sync_engine, started.task_id)["offered_at"] is not None
    await conv.end(f"Great. By the way: {summary} Want the link?")
    assert await conv.begin("yes please") == []
    assert _report(sync_engine, started.task_id)["delivered_at"] is not None


async def test_on_topic_reply_without_announcement_stays_pending(
    db: Db, report: tuple[Seed, uuid.UUID], sync_engine: Engine
) -> None:
    s, task_id = report
    conv = Conversation(db, s.session_id)
    assert len(await conv.begin("hi")) == 1
    # Shares most content words with the summary but never announces it.
    await conv.end("So for the vineyards near Napa, do you want autumn availability?")
    again = await conv.begin("maybe")
    assert _report(sync_engine, task_id)["delivered_at"] is None
    assert len(again) == 1 and again[0].startswith("Not mentioned yet. Briefly")


async def test_paraphrase_is_not_confirmed_but_exact_sentence_is(
    db: Db, report: tuple[Seed, uuid.UUID], sync_engine: Engine
) -> None:
    s, task_id = report
    conv = Conversation(db, s.session_id)
    await conv.begin("hi")
    await conv.end("I found 3 vineyard options in Napa that are free in the fall.")
    assert len(await conv.begin("ok")) == 1
    assert _report(sync_engine, task_id)["delivered_at"] is None
    # Case, punctuation and whitespace differences are fine.
    await conv.end("Also -- found THREE vineyards with autumn   availability near Napa")
    assert await conv.begin("thanks") == []
    assert _report(sync_engine, task_id)["delivered_at"] is not None


async def test_barge_in_mid_announcement_keeps_report_pending_and_reoffers_briefly(
    db: Db, report: tuple[Seed, uuid.UUID], sync_engine: Engine
) -> None:
    s, task_id = report
    conv = Conversation(db, s.session_id)
    first = await conv.begin("hi")
    generated = f"Quick update: {SUMMARY} Shall I send it?"
    # The user cut in inside the announcement; ElevenLabs recorded only the spoken part.
    await conv.end(generated, recorded="Quick update: Found three vineyards with autumn")
    second = await conv.begin("wait, one thing")
    assert _report(sync_engine, task_id)["delivered_at"] is None
    assert len(second) == 1 and second[0].startswith("Your earlier mention was cut off.")
    assert len(second[0]) < len(first[0])
    # Cut after the announcement (before "Shall I send it?") counts as delivered.
    await conv.end(generated, recorded=f"Quick update: {SUMMARY} Shall")
    assert await conv.begin("nice") == []
    assert _report(sync_engine, task_id)["delivered_at"] is not None


async def test_reoffers_are_capped_at_two(
    db: Db, report: tuple[Seed, uuid.UUID], sync_engine: Engine
) -> None:
    s, task_id = report
    conv = Conversation(db, s.session_id)
    offered = []
    for turn in range(4):
        offered.append(len(await conv.begin(f"turn {turn}")))
        await conv.end("Let's keep talking about the menu.")
    assert offered == [1, 1, 1, 0]  # first offer + 2 brief re-offers, then give up
    assert _report(sync_engine, task_id)["delivered_at"] is not None


async def test_no_offer_on_tool_result_follow_up(
    db: Db, report: tuple[Seed, uuid.UUID], sync_engine: Engine
) -> None:
    s, task_id = report
    hook = ReportsHook()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "what's the status?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "It is done."},
    ]
    ctx = TurnContext(
        state=SessionState(session_id=s.session_id),
        db=db,
        settings=Settings(),
        messages=messages,
        user_text="what's the status?",
    )
    assert await hook.before_model(ctx) == []
    assert _report(sync_engine, task_id)["offered_at"] is None
