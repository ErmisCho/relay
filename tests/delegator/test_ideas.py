"""Idea recall, focus/attribution, edges and summary regeneration against the real database."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import (
    SessionState,
    SessionStore,
    ToolContext,
    ToolRegistry,
    ToolResult,
    TurnContext,
)
from relay.delegator.hooks.ideas import IdeaSummaryHook
from relay.delegator.tools.ideas import (
    RECALL_MAX_CHARS,
    FocusIdeaTool,
    LinkIdeasTool,
    RecallTool,
    same_idea_title,
)
from relay.delegator.wiring import build_hooks, build_registry
from relay.store import ideas_repo
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Artifact, Commitment, Idea, Session, Task, Turn

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text
from .test_tools import tool_call


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


def tool_ctx(
    db: async_sessionmaker[AsyncSession], state: SessionState | None = None
) -> ToolContext:
    return ToolContext(state or SessionState(uuid.uuid4()), db, make_settings())


def word() -> str:
    """A unique, letters-only token (the shared test DB keeps ideas from other tests)."""
    return "zq" + "".join("abcdefghijklmnop"[int(c, 16)] for c in uuid.uuid4().hex[:10])


async def test_recall_in_new_session_returns_summary_commitments_and_artifacts(
    db: async_sessionmaker[AsyncSession],
) -> None:
    tag = word()
    idea_id = await ideas_repo.create_idea(db, f"Routing heuristics {tag}")
    await ideas_repo.set_summary(db, idea_id, "Route easy turns to the small local model.")
    async with db() as s, s.begin():
        c = Commitment(
            idea_id=idea_id,
            goal="Survey routing papers",
            scope_excludes="no code",
            artifact_kind="document",
            readback_text="…",
            assent_utterance="yes",
            assented_at=datetime.now(UTC),
        )
        s.add(c)
        await s.flush()
        t = Task(commitment_id=c.id, kind="research", status="succeeded")
        s.add(t)
        await s.flush()
        s.add(
            Artifact(
                task_id=t.id,
                kind="document",
                url="https://docs.example/routing",
                summary="Three routing strategies compared.",
            )
        )

    # Session B: fresh state, nothing focused (decision 2: no automatic resume).
    state = SessionState(uuid.uuid4())
    result = await RecallTool()(
        {"query": f"where did we land on the {tag} routing"}, tool_ctx(db, state)
    )

    assert not result.rejected
    for expected in (
        str(idea_id),
        "small local model",
        "Survey routing papers",
        "succeeded",
        "https://docs.example/routing",
        "Three routing strategies compared.",
    ):
        assert expected in result.content
    assert state.current_idea_id is None


async def test_fts_matches_stemmed_words(db: async_sessionmaker[AsyncSession]) -> None:
    tag = word()
    idea_id = await ideas_repo.create_idea(db, f"Batching notifications {tag}")
    await ideas_repo.set_summary(db, idea_id, "Deliver summaries when the user is walking.")

    found = await ideas_repo.search_ideas(db, f"{tag} notification walks", 3)
    assert [i.id for i in found] == [idea_id]


async def test_recall_output_stays_under_cap_with_many_ideas(
    db: async_sessionmaker[AsyncSession],
) -> None:
    tag = word()
    for n in range(12):
        i = await ideas_repo.create_idea(db, f"{tag} variant {n} " + "long title " * 20)
        await ideas_repo.set_summary(db, i, f"{tag} " + "a very long summary sentence. " * 80)
    result = await RecallTool(max_ideas=10)({"query": tag}, tool_ctx(db))
    assert "Summary:" in result.content and len(result.content) <= RECALL_MAX_CHARS


async def test_link_ideas_is_recalled_and_invalid_relation_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    tag = word()
    old = await ideas_repo.create_idea(db, f"Old pairing flow {tag}")
    new = await ideas_repo.create_idea(db, "QR pairing flow")
    link = LinkIdeasTool()
    args = {"from_idea_id": str(new), "to_idea_id": str(old), "relation": "supersedes"}

    first = await link(args, tool_ctx(db))
    again = await link(args, tool_ctx(db))
    assert not first.rejected and first.content.startswith("Noted")
    assert again.content.startswith("Already noted")
    bad = await link({**args, "relation": "likes"}, tool_ctx(db))
    missing = await link({**args, "to_idea_id": str(uuid.uuid4())}, tool_ctx(db))
    assert bad.rejected and missing.rejected

    recalled = (await RecallTool()({"query": tag}, tool_ctx(db))).content
    assert f'"QR pairing flow" ({new}) supersedes this' in recalled


async def test_focus_idea_attributes_turns_persisted_by_the_app(
    db: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    registry = ToolRegistry()
    registry.register(FocusIdeaTool())
    model = ScriptedChatModel(
        [
            tool_call("focus_idea", '{"new_title": "Phone-proximity bike lock"}'),
            text("Let's dig in."),
            text("Who rides it?"),
        ]
    )
    store = SessionStore()
    app = create_app(
        make_settings(),
        chat_model=model,
        registry=registry,
        hooks=[],
        session_store=store,
        sessionmaker=db,
    )
    body = load_request()
    body["elevenlabs_extra_body"]["session_id"] = str(session_id)
    await post(app, body)
    body["messages"] += [
        {"role": "assistant", "content": "Let's dig in."},
        {"role": "user", "content": "Mostly commuters."},
    ]
    await post(app, body)

    idea_id = store.get(session_id).current_idea_id
    assert idea_id is not None
    async with db() as s:
        idea = await s.get(Idea, idea_id)
        turns = (await s.scalars(select(Turn).where(Turn.session_id == session_id))).all()
    assert idea is not None and idea.status == "exploring"
    # Both user turns (the first one retroactively) and both assistant turns.
    assert len(turns) == 4 and {t.idea_id for t in turns} == {idea_id}


async def test_summary_hook_runs_on_nth_turn_and_on_idea_switch_only(
    db: async_sessionmaker[AsyncSession],
) -> None:
    first = await ideas_repo.create_idea(db, "Voice journaling")
    second = await ideas_repo.create_idea(db, "Standing desk timer")
    sid = uuid.uuid4()
    async with db() as s, s.begin():
        s.add(Session(id=sid))
        await s.flush()
        s.add_all(
            [
                Turn(session_id=sid, idea_id=i, role="user", text="some thoughts")
                for i in (first, second)
            ]
        )

    model = ScriptedChatModel([text("Summary %d." % n) for n in range(10)])
    hook = IdeaSummaryHook(3, model=model, idle_s=60)  # drain() flushes what is due
    state = SessionState(sid, current_idea_id=first)
    ctx = TurnContext(state, db, make_settings(), messages=[], user_text="")

    async def turn() -> None:
        state.user_turn_index += 1
        await hook.after_response(ctx, "ok")
        await hook.drain()

    await turn()  # first focus of the session is not a switch: nothing to summarise yet
    await turn()
    await turn()
    assert model.calls == []
    await turn()  # 3 user turns since focusing
    assert len(model.calls) == 1
    state.current_idea_id = second
    await turn()  # switch: the new idea and the one being left
    assert len(model.calls) == 3
    await turn()  # same idea again: not a switch
    assert len(model.calls) == 3
    async with db() as s:
        assert (await s.get(Idea, second)).summary == "Summary 1."  # type: ignore[union-attr]


async def _idea_with_turn(db: async_sessionmaker[AsyncSession]) -> SessionState:
    idea_id = await ideas_repo.create_idea(db, "Pocket weather station")
    sid = uuid.uuid4()
    async with db() as s, s.begin():
        s.add(Session(id=sid))
        await s.flush()
        s.add(Turn(session_id=sid, idea_id=idea_id, role="user", text="it should fit a pocket"))
    return SessionState(sid, current_idea_id=idea_id)


async def test_summary_waits_for_idle_and_never_runs_during_active_turns(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: a background summary took the single Ollama slot during a live turn."""
    state = await _idea_with_turn(db)
    model = ScriptedChatModel([text("Summary.") for _ in range(5)])
    hook = IdeaSummaryHook(2, model=model, idle_s=0.3)
    msgs = [{"role": "user", "content": "more"}]
    ctx = TurnContext(state, db, make_settings(), messages=msgs, user_text="more")
    for _ in range(8):  # turns every 0.05 s: summaries become due but the session never idles
        await hook.before_model(ctx)
        state.user_turn_index += 1
        await hook.after_response(ctx, "ok")
        await asyncio.sleep(0.05)
    assert model.calls == []
    await asyncio.sleep(0.6)
    assert len(model.calls) == 1
    async with db() as s:
        idea = await s.get(Idea, state.current_idea_id)
    assert idea is not None and idea.summary == "Summary."


async def test_drain_runs_a_due_summary_immediately(db: async_sessionmaker[AsyncSession]) -> None:
    state = await _idea_with_turn(db)
    model = ScriptedChatModel([text("Drained.")])
    hook = IdeaSummaryHook(1, model=model, idle_s=3600)
    ctx = TurnContext(state, db, make_settings(), messages=[], user_text="")
    for _ in range(2):  # first focus starts the count; the next turn makes it due
        state.user_turn_index += 1
        await hook.after_response(ctx, "ok")
    assert model.calls == []
    await hook.drain()
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("new", "current", "same"),
    [
        ("Bike Lock Idea", "Phone-proximity bike lock", True),
        ("phone proximity bike locks", "Phone-proximity bike lock", True),
        ("Bike lock marketing plan", "Bike lock", False),
        ("Phone app", "Phone-proximity bike lock app", False),
        ("Standing desk timer", "Phone-proximity bike lock", False),
        ("Phone-proximity bike lock using BLE", "Phone-proximity bike lock", True),
    ],
)
def test_same_idea_title_keeps_spin_offs_separate(new: str, current: str, same: bool) -> None:
    assert same_idea_title(new, current) is same


async def test_focus_idea_does_not_duplicate_and_reattributes_the_switching_turn(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: a model calling focus_idea every turn created duplicate ideas."""
    tag = word()
    state = SessionState(uuid.uuid4())
    async with db() as s, s.begin():
        s.add(Session(id=state.session_id))
        await s.flush()
        user_turn = Turn(session_id=state.session_id, role="user", text="hi")
        s.add(user_turn)
    ctx = ToolContext(state, db, make_settings(), user_turn_id=user_turn.id)
    focus = FocusIdeaTool()

    await focus({"new_title": f"Phone-proximity bike lock {tag}"}, ctx)
    first = state.current_idea_id
    assert first is not None
    assert (await focus({"idea_id": str(first)}, ctx)).content.startswith("Already")
    assert (await focus({"new_title": f"Bike Lock Idea {tag}"}, ctx)).content.startswith("Already")
    await focus({"new_title": f"Standing desk timer {tag}"}, ctx)
    second = state.current_idea_id
    assert second not in (None, first)
    async with db() as s:
        assert (await s.get(Turn, user_turn.id)).idea_id == second  # type: ignore[union-attr]
    await focus({"new_title": f"  phone-proximity BIKE lock {tag} "}, ctx)
    assert state.current_idea_id == first
    async with db() as s:
        ideas = (await s.scalars(select(Idea).where(Idea.title.contains(tag)))).all()
    assert len(ideas) == 2


@pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="set RELAY_LLM_TESTS=1 to hit a live model"
)
async def test_live_focus_idea_creates_one_idea_and_repeats_are_no_ops(
    db: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    results: list[str] = []
    original = FocusIdeaTool.__call__

    async def counting(self: FocusIdeaTool, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append(args)
        result = await original(self, args, ctx)
        results.append(result.content)
        return result

    monkeypatch.setattr(FocusIdeaTool, "__call__", counting)
    settings = make_settings(
        delegator_model="ollama:gemma4:e4b", delegator_fallback_model="ollama:gemma4:e4b"
    )
    hooks = build_hooks()
    app = create_app(
        settings, registry=build_registry(), hooks=hooks, sessionmaker=db, warm_dbos=False
    )
    caplog.set_level(logging.INFO, logger="relay.delegator.service")
    session_id = uuid.uuid4()
    started = datetime.now(UTC)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are relay, a voice-first thinking partner."}
    ]
    ttfts: list[Any] = []
    for said in (
        "I have an idea for a bike lock that unlocks when my phone is nearby.",
        "It would use Bluetooth low energy to detect the phone.",
        "Battery life worries me, it has to last months.",
        "Maybe a small solar panel on the lock body could help.",
    ):
        messages.append({"role": "user", "content": said})
        body = {**load_request(), "messages": messages}
        body["elevenlabs_extra_body"] = {
            **body["elevenlabs_extra_body"],
            "session_id": str(session_id),
        }
        caplog.clear()
        chunks = parse_sse((await post(app, body)).content)
        reply = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        messages.append({"role": "assistant", "content": reply})
        (record,) = [r for r in caplog.records if r.getMessage().startswith("delegator turn")]
        ttfts.append(record.args[1])  # type: ignore[index]
    for hook in hooks:
        if isinstance(hook, IdeaSummaryHook):
            await hook.drain()
    async with db() as s:
        n_ideas = len((await s.scalars(select(Idea.id).where(Idea.created_at >= started))).all())
    print(f"\nfocus_idea calls: {calls}; new ideas: {n_ideas}; TTFT ms per turn: {ttfts}")
    # gemma4:e4b sometimes repeats focus_idea with the same title (measured: 6/8 runs).
    # That is harmless as long as the server treats it as a no-op: exactly one idea,
    # and every call after the first answers "Already on …" without switching.
    assert n_ideas == 1
    assert calls, "the model never focused an idea"
    assert all(r.startswith("Already on") for r in results[1:]), results
    assert len(calls) <= 3, calls
