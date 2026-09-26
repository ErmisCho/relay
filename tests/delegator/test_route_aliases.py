"""ElevenLabs may treat the configured Server URL as a base or as the full endpoint."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import CHAT_COMPLETIONS_PATHS, create_app
from relay.delegator.contracts import ToolRegistry

from .conftest import AUTH, ScriptedChatModel, load_request, make_settings, parse_sse, text


@pytest.mark.parametrize("path", CHAT_COMPLETIONS_PATHS)
async def test_every_url_interpretation_streams_and_requires_auth(
    path: str, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    model = ScriptedChatModel([text("hi")])
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauth = await client.post(path, json=load_request())
        resp = await client.post(path, json=load_request(), headers=AUTH)

    assert unauth.status_code == 401
    assert resp.status_code == 200
    assert "".join(
        c["choices"][0]["delta"].get("content") or "" for c in parse_sse(resp.content)
    ) == "hi"
    await app.state.service.drain()
