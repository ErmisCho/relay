"""Delegator test helpers: a scripted upstream model and an ASGI client helper."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.llm import ChatDelta
from relay.store.db import create_engine, create_sessionmaker

SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
FIXTURE = Path(__file__).parent / "fixtures" / "elevenlabs_custom_llm_request.json"
# Nothing listens on port 1: every DB write fails fast, exercising the "DB down" path.
DEAD_DB_URL = "postgresql+asyncpg://relay:relay@127.0.0.1:1/relay"


def load_request() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "delegator_shared_secret": SECRET,
        "delegator_public_url": "http://localhost:8000",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


Script = list[ChatDelta] | Exception


class ScriptedChatModel:
    """Fake upstream: each ``stream`` call plays the next script (or raises it)."""

    def __init__(self, scripts: list[Script], name: str = "fake:model") -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []
        self._name = name

    @property
    def model_name(self) -> str:
        return self._name

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> AsyncIterator[ChatDelta]:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tool_choice": tool_choice,
            }
        )
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        for delta in script:
            yield delta


def text(*parts: str) -> list[ChatDelta]:
    return [ChatDelta(content=p) for p in parts] + [ChatDelta(finish_reason="stop")]


async def post(
    app: FastAPI, body: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions", json=body, headers=AUTH if headers is None else headers
        )
    # Turn finalisation (persistence, after_response) runs detached; let it finish.
    await app.state.service.drain()
    return resp


def parse_sse(body: bytes) -> list[dict[str, Any]]:
    """Strictly parse an OpenAI SSE body; asserts the framing byte-for-byte."""
    assert body.endswith(b"data: [DONE]\n\n")
    frames = body[: -len(b"data: [DONE]\n\n")].split(b"\n\n")
    assert frames[-1] == b""
    chunks = []
    for frame in frames[:-1]:
        assert frame.startswith(b"data: ")
        assert b"\n" not in frame
        chunks.append(json.loads(frame[len(b"data: ") :]))
    return chunks


@pytest.fixture
def dead_db() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(DEAD_DB_URL)
    yield create_sessionmaker(engine)
