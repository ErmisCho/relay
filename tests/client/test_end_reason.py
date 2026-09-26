"""sessions.end_reason through the real listener, ElevenLabs session (fake SDK) and Postgres.

Bug each test pins: the audit cannot tell a silence auto-close from the agent hanging up (or a
stale reconcile) if a path writes `ended_at` without its reason, or reports the wrong one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.client.elevenlabs_session import ElevenLabsVoiceSession, SounddeviceAudioInterface
from relay.client.listener import PostgresSessionStore, WakeListener
from relay.config import Settings
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Session
from tests.client.fakes import ConversationRecorder, StreamRecorder
from tests.client.test_listener import FakeAudio, FakeDetector, wait_for


@pytest.fixture
async def maker(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def _reason(maker: async_sessionmaker[AsyncSession], sid: str) -> str | None:
    async with maker() as db:
        row = (await db.execute(select(Session).where(Session.id == uuid.UUID(sid)))).scalar_one()
    return row.end_reason if row.ended_at is not None else "(still open)"


def _listener(
    maker: async_sessionmaker[AsyncSession], silence_timeout_s: float
) -> tuple[WakeListener, ElevenLabsVoiceSession, ConversationRecorder, FakeAudio]:
    conversations = ConversationRecorder()
    voice = ElevenLabsVoiceSession(
        Settings(_env_file=None, elevenlabs_agent_id="agent_test"),  # type: ignore[call-arg]
        conversation_factory=conversations,
        audio_interface_factory=lambda: SounddeviceAudioInterface(
            input_stream_factory=StreamRecorder(), output_stream_factory=StreamRecorder()
        ),
    )
    audio = FakeAudio([])
    listener = WakeListener(
        detector=FakeDetector({2}),
        audio=audio,
        voice=voice,
        store=PostgresSessionStore(maker),
        silence_timeout_s=silence_timeout_s,
        watchdog_interval_s=0.02,
    )
    return listener, voice, conversations, audio


async def test_agent_end_call_records_server_closed(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    listener, voice, conversations, audio = _listener(maker, silence_timeout_s=60)
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: voice.active)
        sid = conversations.conversations[0].kwargs["config"].dynamic_variables["session_id"]
        conversations.conversations[0].server_closed()  # agent end_call closes the socket
        await wait_for(lambda: audio.is_open)  # back to listening after _finish
        assert await _reason(maker, sid) == "server_closed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_silence_auto_close_records_silence_timeout(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    listener, voice, conversations, audio = _listener(maker, silence_timeout_s=0.1)
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: bool(conversations.conversations))
        sid = conversations.conversations[0].kwargs["config"].dynamic_variables["session_id"]
        await wait_for(lambda: audio.is_open)
        # The SDK's end callback fires inside our stop(); it must not relabel it server_closed.
        assert voice.end_reason == "stopped"
        assert await _reason(maker, sid) == "silence_timeout"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_reconcile_records_its_reason(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.uuid4()
    async with maker.begin() as db:
        started = datetime.now(UTC) - timedelta(hours=2)
        await db.execute(insert(Session).values(id=sid, started_at=started, wake_trigger="x"))

    assert await PostgresSessionStore(maker).end_stale(60) >= 1

    assert await _reason(maker, str(sid)) == "stale_reconciled"
