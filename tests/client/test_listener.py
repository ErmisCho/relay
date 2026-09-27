"""The wake -> session -> auto-close -> wake loop, with fakes and the real sessions table."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.client.__main__ import run_until_signalled
from relay.client.listener import PostgresSessionStore, WakeListener
from relay.client.voice import EndCallback, TranscriptCallback, VoiceSession
from relay.client.wake import FRAME_SAMPLES, Frame
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Session


class FakeDetector:
    name = "hey_jarvis"

    def __init__(self, trigger_frames: set[int]) -> None:
        self.trigger_frames = trigger_frames
        self.frames = 0
        self.resets = 0

    def process(self, frame: Frame) -> bool:
        self.frames += 1
        return self.frames in self.trigger_frames

    def reset(self) -> None:
        self.resets += 1


class FakeAudio:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.is_open = False

    async def open(self) -> None:
        self.log.append("mic-open")
        self.is_open = True

    async def read(self) -> Frame:
        assert self.is_open, "read from a closed microphone"
        await asyncio.sleep(0.001)
        return np.zeros(FRAME_SAMPLES, dtype=np.int16)

    async def close(self) -> None:
        if self.is_open:
            self.log.append("mic-close")
        self.is_open = False


class FakeVoice:
    """Records calls; `end_call()` simulates the agent ending, from an SDK-like thread."""

    def __init__(
        self,
        log: list[str],
        audio: FakeAudio,
        fail_starts: int = 0,
        *,
        connect_delay_s: float = 0.0,
        stop_delay_s: float = 0.0,
    ) -> None:
        self.log = log
        self.connect_delay_s = connect_delay_s
        self.stop_delay_s = stop_delay_s
        self.stopping = threading.Event()
        self.audio = audio
        self.fail_starts = fail_starts
        self.on_user_transcript: TranscriptCallback | None = None
        self.on_agent_response: TranscriptCallback | None = None
        self.on_end: EndCallback | None = None
        self.last_activity_ts = time.monotonic()
        self.holds_microphone = False
        self.active = False
        self.session_ids: list[str] = []
        self.start_mic_was_open: list[bool] = []
        self.stops = 0

    def prepare(self) -> None:
        self.log.append("prepare")

    def start(self, session_id: str) -> None:
        self.log.append("start")
        self.start_mic_was_open.append(self.audio.is_open)
        self.session_ids.append(session_id)
        if self.fail_starts:
            self.fail_starts -= 1
            raise ConnectionError("no network")
        self.last_activity_ts = time.monotonic()
        self.active = True
        if self.connect_delay_s:  # the socket connects after start() returns, like the SDK
            threading.Timer(self.connect_delay_s, self._connect).start()
        else:
            self._connect()

    def _connect(self) -> None:
        if self.active:
            self.holds_microphone = True

    def _end(self) -> None:
        if not self.active:
            return
        self.active = False
        self.holds_microphone = False
        self.log.append("voice-released")
        assert self.on_end is not None
        self.on_end()

    def stop(self) -> None:
        self.stops += 1
        self.stopping.set()
        time.sleep(self.stop_delay_s)  # the real stop() joins the socket thread: 0.3-0.8 s
        self._end()

    def end_call(self) -> None:
        t = threading.Thread(target=self._end)
        t.start()
        t.join()


@pytest.fixture
async def sessionmaker(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


async def row(maker: async_sessionmaker[AsyncSession], sid: str) -> Session:
    async with maker() as db:
        return (await db.execute(select(Session).where(Session.id == uuid.UUID(sid)))).scalar_one()


def make(
    maker: async_sessionmaker[AsyncSession],
    triggers: set[int],
    *,
    fail_starts: int = 0,
    silence_timeout_s: float = 60,
    connect_delay_s: float = 0.0,
    stop_delay_s: float = 0.0,
) -> tuple[WakeListener, FakeVoice, FakeAudio, list[str]]:
    log: list[str] = []
    audio = FakeAudio(log)
    voice = FakeVoice(
        log, audio, fail_starts, connect_delay_s=connect_delay_s, stop_delay_s=stop_delay_s
    )
    assert isinstance(voice, VoiceSession)
    listener = WakeListener(
        detector=FakeDetector(triggers),
        audio=audio,
        voice=voice,
        store=PostgresSessionStore(maker),
        silence_timeout_s=silence_timeout_s,
        watchdog_interval_s=0.02,
    )
    return listener, voice, audio, log


async def test_trigger_hands_mic_to_session_fast_and_end_call_resumes(
    sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="relay.client.listener")
    listener, voice, audio, log = make(sessionmaker, {3}, connect_delay_s=0.15)
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: voice.holds_microphone)
        await wait_for(lambda: bool(listener.trigger_to_connected_ms))
        # AC1 (with fakes): latency runs to CONNECTED (after the 150 ms connect), not to the
        # start() call, and stays under 500 ms.
        assert listener.trigger_to_start_ms[0] < 150
        assert 150 <= listener.trigger_to_connected_ms[0] < 500
        assert "trigger -> connected" in caplog.text
        # AC4: the listener's stream was closed before start() and stays closed while active.
        assert voice.start_mic_was_open == [False]
        assert not audio.is_open
        created = await row(sessionmaker, voice.session_ids[0])
        assert created.wake_trigger == "hey_jarvis" and created.ended_at is None

        voice.end_call()  # the agent's end_call tool ("that's all")
        await wait_for(lambda: log.count("prepare") == 2)
        assert log == [
            "mic-open",
            "prepare",
            "mic-close",
            "start",
            "voice-released",
            "mic-open",
            "prepare",
        ]
        assert (await row(sessionmaker, voice.session_ids[0])).ended_at is not None
        assert voice.stops == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_silence_auto_closes_and_a_second_session_opens(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    listener, voice, audio, log = make(sessionmaker, {2, 5}, silence_timeout_s=0.1)
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: len(voice.session_ids) == 2 and not voice.active)
        await wait_for(lambda: audio.is_open)
        assert voice.stops == 2  # closed by the watchdog both times, not by the agent
        for sid in voice.session_ids:
            assert (await row(sessionmaker, sid)).ended_at is not None
        assert voice.start_mic_was_open == [False, False]
        assert log.count("mic-open") == 3
        # TASK-24 AC1: every return to LISTENING (after each session too) prefetches the next
        # signed URL before the trigger, so no session waits on get_signed_url.
        await wait_for(lambda: log.count("prepare") == 3)
        assert [e for e in log if e in ("prepare", "start")] == [
            "prepare",
            "start",
            "prepare",
            "start",
            "prepare",
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_start_ends_row_and_resumes_listening(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    listener, voice, audio, _ = make(sessionmaker, {2, 4}, fail_starts=1)
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: voice.active)  # the second trigger worked
        assert len(voice.session_ids) == 2
        assert (await row(sessionmaker, voice.session_ids[0])).ended_at is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_stops_an_active_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Ctrl-C must not leave a session billing: the voice session is stopped and the row ended.
    listener, voice, audio, _ = make(sessionmaker, {2})
    task = asyncio.create_task(listener.run())
    await wait_for(lambda: voice.active)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not voice.active and voice.stops == 1
    assert not audio.is_open
    assert (await row(sessionmaker, voice.session_ids[0])).ended_at is not None


async def test_double_interrupt_during_slow_stop_still_sets_ended_at(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # `uv run` turns one Ctrl-C into two SIGINTs; the second landed while stop() was joining
    # the socket thread and abandoned the ended_at write (live session 8de4fd25).
    listener, voice, _, _ = make(sessionmaker, {2}, stop_delay_s=0.3)
    task = asyncio.create_task(listener.run())
    await wait_for(lambda: voice.holds_microphone)
    task.cancel()
    await asyncio.to_thread(voice.stopping.wait, 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await row(sessionmaker, voice.session_ids[0])).ended_at is not None
    assert not voice.holds_microphone and voice.stops == 1


async def test_two_real_sigints_shut_down_gracefully(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    listener, voice, audio, _ = make(sessionmaker, {2}, stop_delay_s=0.3)
    runner = asyncio.create_task(run_until_signalled(listener.run()))
    await wait_for(lambda: voice.holds_microphone)
    signal.raise_signal(signal.SIGINT)
    await asyncio.to_thread(voice.stopping.wait, 5)
    signal.raise_signal(signal.SIGINT)  # ignored, not a KeyboardInterrupt
    await runner
    assert (await row(sessionmaker, voice.session_ids[0])).ended_at is not None
    assert not voice.holds_microphone and not audio.is_open


async def test_startup_ends_stale_wake_sessions_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A crashed or killed client leaves ended_at NULL forever; the next start closes such rows.
    stale, fresh, delegator = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as db:
        await db.execute(
            insert(Session).values(
                id=stale,
                wake_trigger="hey_jarvis",
                started_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        await db.execute(insert(Session).values(id=fresh, wake_trigger="hey_jarvis"))
        await db.execute(
            insert(Session).values(id=delegator, started_at=datetime.now(UTC) - timedelta(hours=1))
        )
    listener, _, audio, _ = make(sessionmaker, set())
    task = asyncio.create_task(listener.run())
    try:
        await wait_for(lambda: audio.is_open)
        stale_row = await row(sessionmaker, str(stale))
        assert stale_row.ended_at == stale_row.started_at  # closed at its last activity
        assert (await row(sessionmaker, str(fresh))).ended_at is None
        assert (await row(sessionmaker, str(delegator))).ended_at is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
