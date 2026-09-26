"""Drive the real Delegator app through scripted voice conversations for commitment tests."""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.commitment import (
    CommitmentHook,
    DispatchTaskTool,
    ProposeCommitmentTool,
    build_readback,
)
from relay.delegator.commitment import protocol as commitment_protocol
from relay.delegator.contracts import SessionStore, ToolRegistry
from relay.delegator.hooks.scope import ScopeHook
from relay.delegator.llm import ChatDelta
from relay.delegator.llm.base import ToolCallDelta
from relay.delegator.scope import ArtifactKind
from relay.delegator.service import ChatCompletionRequest
from relay.executor.dispatch import start_task
from relay.store.models import Commitment, Task

from ..conftest import AUTH, ScriptedChatModel, make_settings, parse_sse, text
from ..test_tools import tool_call

SYSTEM = "You are relay, a voice-first thinking partner."


class FakeLabelModel:
    """Structured-label model double: answers every call with ``answer`` (or raises / stalls)."""

    def __init__(
        self,
        answer: str | Exception | None = "affirmative",
        *,
        delay: float = 0.0,
        as_text: str | None = None,
        as_tool: bool = False,
    ) -> None:
        self.answer = answer
        self.delay = delay
        self.as_text = as_text
        self.as_tool = as_tool
        self.calls: list[list[dict[str, Any]]] = []
        self.on_call: Any = None

    @property
    def model_name(self) -> str:
        return "fake:label"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> Any:
        self.calls.append(messages)
        if self.on_call is not None:
            self.on_call()
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.as_text is not None:
            yield ChatDelta(content=self.as_text)
            return
        if not self.as_tool:
            yield ChatDelta(content=json.dumps({"label": self.answer}))
            return
        name = "classify_assent"
        yield ChatDelta(
            tool_calls=[
                ToolCallDelta(0, id="c1", name=name, arguments=json.dumps({"label": self.answer}))
            ]
        )
        yield ChatDelta(finish_reason="tool_calls")


class StubDBOSClient:
    def __init__(self) -> None:
        self.enqueued: list[tuple[Any, ...]] = []

    def enqueue(self, options: Any, *args: Any) -> None:
        self.enqueued.append((options, *args))


async def post(app: FastAPI, body: dict[str, Any]) -> httpx.Response:
    """Like ``conftest.post`` but without ``service.drain()``: drain now CANCELS reserved
    dispatches (shutdown semantics), so tests wait for turn finalisation only."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
    await finalised(app)
    return resp


async def finalised(app: FastAPI) -> None:
    pending = set(app.state.service._background)
    if pending:
        await asyncio.wait(pending)


async def settle() -> None:
    """Wait until every reservation has committed or been cancelled (grace elapsed)."""
    while commitment_protocol._BACKGROUND:
        await asyncio.wait(set(commitment_protocol._BACKGROUND))


def word() -> str:
    return "zq" + "".join("abcdefghijklmnop"[int(c, 16)] for c in uuid.uuid4().hex[:10])


@dataclass
class Convo:
    """One voice session against the real app. ``history`` is what ElevenLabs would resend."""

    app: FastAPI
    chat: ScriptedChatModel
    assent: FakeLabelModel
    db: async_sessionmaker[AsyncSession]
    store: SessionStore
    client: StubDBOSClient
    tag: str = field(default_factory=word)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: list[dict[str, Any]] = field(
        default_factory=lambda: [{"role": "system", "content": SYSTEM}]
    )

    @property
    def goal(self) -> str:
        return f"research {self.tag} bike locks"

    def readback(
        self,
        goal: str | None = None,
        excludes: str = "pricing",
        artifact: ArtifactKind = ArtifactKind.DOCUMENT,
    ) -> str:
        return build_readback(goal or self.goal, excludes, artifact)

    async def turn(self, user: str, *scripts: list[ChatDelta], recorded: str | None = None) -> str:
        """One user turn. ``recorded`` overrides what ElevenLabs records as spoken (barge-in)."""
        self.history.append({"role": "user", "content": user})
        return await self._post(scripts, recorded)

    async def resend(
        self, user: str | None, *scripts: list[ChatDelta], recorded: str | None = None
    ) -> str:
        """ElevenLabs re-sends the latest user turn (after a false barge-in), maybe extended."""
        if self.history[-1]["role"] == "assistant":
            self.history.pop()
        if user is not None:
            self.history[-1] = {"role": "user", "content": user}
        return await self._post(scripts, recorded)

    async def cut_turn(self, user: str, *scripts: list[ChatDelta], frames: int = 1) -> None:
        """A user turn whose SSE response the client drops after ``frames`` frames (barge-in).

        Nothing is appended as the assistant's message: a re-send follows (see ``resend``).
        """
        self.history.append({"role": "user", "content": user})
        self.chat.scripts = list(scripts)
        svc = self.app.state.service
        body = ChatCompletionRequest(
            model="relay-delegator",
            messages=copy.deepcopy(self.history),
            stream=True,
            elevenlabs_extra_body={"session_id": self.session_id},
        )
        turn = await svc.prepare(body, {}, time.perf_counter())
        frames_iter = svc.stream_sse(turn)
        for _ in range(frames):
            await frames_iter.__anext__()
        await frames_iter.aclose()
        await asyncio.sleep(0)

    async def _post(self, scripts: tuple[list[ChatDelta], ...], recorded: str | None) -> str:
        self.chat.scripts = list(scripts)
        body = {
            "model": "relay-delegator",
            "messages": self.history,
            "stream": True,
            "elevenlabs_extra_body": {"session_id": self.session_id},
        }
        resp = await post(self.app, body)
        assert resp.status_code == 200
        spoken = "".join(
            c["choices"][0]["delta"].get("content") or "" for c in parse_sse(resp.content)
        )
        self.history.append(
            {"role": "assistant", "content": spoken if recorded is None else recorded}
        )
        return spoken

    def tool_results(self) -> list[str]:
        """Tool results the model saw in the last turn, in order."""
        last = self.chat.calls[-1]["messages"] if self.chat.calls else []
        return [m["content"] for m in last if m.get("role") == "tool"]

    def notes(self) -> list[str]:
        """Per-turn system notes the model saw in the first round of the last turn."""
        first = next(
            c
            for c in reversed(self.chat.calls)
            if not any(m.get("role") == "tool" for m in c["messages"])
        )
        return [m["content"] for m in first["messages"][2:] if m.get("role") == "system"]

    async def commitments(self) -> list[Commitment]:
        await settle()
        async with self.db() as s:
            return list(
                await s.scalars(select(Commitment).where(Commitment.goal.contains(self.tag)))
            )

    async def task_count(self) -> int:
        await settle()
        async with self.db() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(Task)
                .join(Commitment, Commitment.id == Task.commitment_id)
                .where(Commitment.goal.contains(self.tag))
            )
        return int(n or 0)

    async def assert_nothing_dispatched(self) -> None:
        assert await self.commitments() == []
        assert await self.task_count() == 0
        assert self.client.enqueued == []


def build_convo(
    db: async_sessionmaker[AsyncSession],
    *,
    assent: FakeLabelModel | None = None,
    store: SessionStore | None = None,
    hook: CommitmentHook | None = None,
    grace_s: float = 0.2,
    **settings: Any,
) -> Convo:
    assent = assent or FakeLabelModel("affirmative")
    store = store or SessionStore()
    client = StubDBOSClient()
    registry = ToolRegistry()
    registry.register(ProposeCommitmentTool())
    registry.register(
        DispatchTaskTool(functools.partial(start_task, client=client), grace_s=grace_s)
    )
    hook = hook or CommitmentHook(assent_model=assent, score_ready=False)
    chat = ScriptedChatModel([])
    app = create_app(
        make_settings(**settings),
        chat_model=chat,
        registry=registry,
        hooks=[ScopeHook(), hook],
        session_store=store,
        sessionmaker=db,
        warm_dbos=False,
    )
    return Convo(app=app, chat=chat, assent=assent, db=db, store=store, client=client)


def propose(goal: str, excludes: str = "pricing", artifact: str = "document") -> list[ChatDelta]:
    args = {"goal": goal, "scope_excludes": excludes, "artifact_kind": artifact}
    return tool_call("propose_commitment", json.dumps(args))


def dispatch(args: dict[str, Any] | None = None, call_id: str = "call_d") -> list[ChatDelta]:
    return tool_call("dispatch_task", json.dumps(args or {}), call_id)


__all__ = ["text"]
