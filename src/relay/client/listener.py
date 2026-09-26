"""The client state machine: wake word -> voice session -> auto-close -> wake word (SPEC §5, §9).

LISTENING  the listener owns the microphone and feeds 80 ms frames to the wake detector.
ACTIVE     the microphone was handed to the `VoiceSession`; a watchdog closes the session after
           `silence_timeout_s` without transcript activity (a forgotten open session bills
           $4.80/hour). The ElevenLabs server-side timeout is set longer, so this one fires first.

The two never hold the microphone at the same time: the listener's stream is closed before
`VoiceSession.start()` and reopened only once the session reports it released the microphone.
Detector, audio source, voice session and session store are injected so the loop is testable
without audio hardware, a network, or openWakeWord.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

import numpy as np
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.client.voice import VoiceSession
from relay.client.wake import FRAME_SAMPLES, SAMPLE_RATE, Frame, WakeWordDetector
from relay.store.models import Session, Turn

log = logging.getLogger(__name__)


class State(enum.Enum):
    LISTENING = "listening"
    STARTING = "starting"
    ACTIVE = "active"
    CLOSING = "closing"
    STOPPED = "stopped"


class AudioSource(Protocol):
    """The listener's microphone: 16 kHz mono int16 frames of `FRAME_SAMPLES`."""

    @property
    def is_open(self) -> bool: ...

    async def open(self) -> None: ...

    async def read(self) -> Frame:
        """The next frame; only called while open."""
        ...

    async def close(self) -> None:
        """Stop and close the device stream (releases the microphone). Idempotent."""
        ...


class SessionStore(Protocol):
    async def create(self, session_id: uuid.UUID, wake_trigger: str) -> None: ...

    async def end(self, session_id: uuid.UUID) -> None:
        """Set `ended_at` once; later calls keep the first value."""
        ...

    async def end_stale(self, idle_s: float) -> int:
        """End wake-started sessions left open (e.g. by a crash) and idle for over `idle_s`."""
        ...


class PostgresSessionStore:
    """Writes `sessions` rows directly (the client shares Postgres with the Delegator in Phase 1).

    The Delegator later upserts the same id with ON CONFLICT DO NOTHING, so creating first is safe.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create(self, session_id: uuid.UUID, wake_trigger: str) -> None:
        stmt = (
            insert(Session)
            .values(id=session_id, wake_trigger=wake_trigger)
            .on_conflict_do_nothing(index_elements=[Session.id])
        )
        async with self._sessionmaker.begin() as db:
            await db.execute(stmt)

    async def end(self, session_id: uuid.UUID) -> None:
        stmt = (
            update(Session)
            .where(Session.id == session_id, Session.ended_at.is_(None))
            .values(ended_at=func.now())
        )
        async with self._sessionmaker.begin() as db:
            await db.execute(stmt)

    async def end_stale(self, idle_s: float) -> int:
        # Only rows this client created (wake_trigger set). A stale row is closed at its last
        # activity (latest turn, else its start), not at "now", so durations stay honest.
        last_turn = select(func.max(Turn.ts)).where(Turn.session_id == Session.id).scalar_subquery()
        last_activity = func.greatest(
            Session.started_at, func.coalesce(last_turn, Session.started_at)
        )
        stmt = (
            update(Session)
            .where(
                Session.ended_at.is_(None),
                Session.wake_trigger.is_not(None),
                last_activity < func.now() - timedelta(seconds=idle_s),
            )
            .values(ended_at=last_activity)
            .execution_options(synchronize_session=False)
        )
        async with self._sessionmaker.begin() as db:
            result = await db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


class SounddeviceAudioSource:
    """`AudioSource` on a `sounddevice.RawInputStream` (16 kHz, int16, blocksize 1280)."""

    def __init__(self, device: int | str | None = None, *, max_queued_frames: int = 50) -> None:
        self._device = device
        self._max_queued = max_queued_frames
        self._stream: Any = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    async def open(self) -> None:
        if self._stream is not None:
            return
        import sounddevice  # type: ignore[import-untyped]  # lazily: loads PortAudio

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._queue = queue

        def push(data: bytes) -> None:
            if queue.qsize() >= self._max_queued:  # the detector fell behind: drop the oldest
                queue.get_nowait()
            queue.put_nowait(data)

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                log.debug("input stream status: %s", status)
            with contextlib.suppress(RuntimeError):  # loop already closed during shutdown
                loop.call_soon_threadsafe(push, bytes(indata))

        stream = sounddevice.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self._device,
            callback=callback,
        )
        stream.start()
        self._stream = stream

    async def read(self) -> Frame:
        data = await self._queue.get()
        return np.frombuffer(data, dtype=np.int16)

    async def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()  # drop pending buffers; faster than stop() on the trigger path
        finally:
            stream.close()
        self._queue = asyncio.Queue()


StateCallback = Callable[[State], None]


class WakeListener:
    """Runs LISTENING/ACTIVE cycles until cancelled."""

    def __init__(
        self,
        *,
        detector: WakeWordDetector,
        audio: AudioSource,
        voice: VoiceSession,
        store: SessionStore,
        silence_timeout_s: float,
        watchdog_interval_s: float = 1.0,
        mic_release_timeout_s: float = 5.0,
        on_state: StateCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = detector
        self._audio = audio
        self._voice = voice
        self._store = store
        self.silence_timeout_s = silence_timeout_s
        self._watchdog_interval_s = watchdog_interval_s
        self._mic_release_timeout_s = mic_release_timeout_s
        self._on_state = on_state
        self._clock = clock
        self.state = State.STOPPED
        # Per session: trigger -> `VoiceSession.start()` call, and trigger -> connected (the
        # session holds the microphone, i.e. the user can speak; AC1 wants < 500 ms).
        self.trigger_to_start_ms: list[float] = []
        self.trigger_to_connected_ms: list[float] = []
        self._current: uuid.UUID | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None

    def _set_state(self, state: State) -> None:
        self.state = state
        log.info("listener state: %s", state.value)
        if self._on_state is not None:
            self._on_state(state)

    async def run(self) -> None:
        """Listen and open sessions forever; cancel (Ctrl-C) to shut down cleanly.

        Shutdown survives further cancellations: one Ctrl-C under `uv run` can arrive as two
        SIGINTs, and the second must not abandon the `ended_at` write or the voice stop.
        """
        try:
            await self._reconcile_stale()
            while True:
                await self._listen_until_trigger()
                await self._run_session(self._clock())
        finally:
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(self._shutdown())
            while not self._shutdown_task.done():
                try:
                    await asyncio.shield(self._shutdown_task)
                except asyncio.CancelledError:
                    log.info("shutdown already in progress")
            self._shutdown_task.result()

    async def _reconcile_stale(self) -> None:
        try:
            ended = await self._store.end_stale(self.silence_timeout_s)
        except Exception:
            log.exception("could not reconcile stale sessions")
            return
        if ended:
            log.warning("ended %d stale session(s) left open by an earlier run", ended)

    async def _listen_until_trigger(self) -> None:
        await self._audio.open()
        self._set_state(State.LISTENING)
        try:
            self._voice.prepare()  # e.g. prefetch the signed URL; never worth losing the loop
        except Exception:
            log.exception("voice prepare failed; the next session connects without it")
        while True:
            frame = await self._audio.read()
            if self._detector.process(frame):
                log.info("wake word %r detected", self._detector.name)
                return

    async def _run_session(self, trigger_ts: float) -> None:
        session_id = uuid.uuid4()
        self._current = session_id
        self._set_state(State.STARTING)
        # Release the microphone first: the voice session opens its own input stream.
        await self._audio.close()
        try:
            await self._store.create(session_id, self._detector.name)
        except Exception:
            log.exception("could not write sessions row %s; continuing without it", session_id)

        loop = asyncio.get_running_loop()
        ended = asyncio.Event()

        def on_end() -> None:  # provider thread -> event loop
            loop.call_soon_threadsafe(ended.set)

        self._voice.on_end = on_end

        def start() -> None:
            self.trigger_to_start_ms.append((self._clock() - trigger_ts) * 1000)
            self._voice.start(str(session_id))

        # Shielded so a cancellation mid-connect leaves the task for `_shutdown` to await:
        # otherwise the start could complete after shutdown and leave a billing session open.
        self._start_task = asyncio.create_task(asyncio.to_thread(start))
        try:
            await asyncio.shield(self._start_task)
        except Exception:
            log.exception("session %s: voice start failed; back to listening", session_id)
        else:
            self._set_state(State.ACTIVE)
            connected = asyncio.create_task(self._measure_connect(session_id, trigger_ts, ended))
            try:
                await self._watch(session_id, ended)
            finally:
                connected.cancel()
        self._set_state(State.CLOSING)
        await self._finish(session_id)

    async def _measure_connect(
        self, session_id: uuid.UUID, trigger_ts: float, ended: asyncio.Event
    ) -> None:
        """Log trigger -> start() call and trigger -> connected (session holds the mic)."""
        start_ms = self.trigger_to_start_ms[-1]
        while not self._voice.holds_microphone:
            if ended.is_set():
                return
            await asyncio.sleep(0.005)
        connected_ms = (self._clock() - trigger_ts) * 1000
        self.trigger_to_connected_ms.append(connected_ms)
        log.info(
            "session %s: trigger -> start() %.0f ms, trigger -> connected %.0f ms",
            session_id,
            start_ms,
            connected_ms,
        )

    async def _watch(self, session_id: uuid.UUID, ended: asyncio.Event) -> None:
        while not ended.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ended.wait(), self._watchdog_interval_s)
            if ended.is_set():
                log.info("session %s ended by the voice session", session_id)
                return
            idle = self._clock() - self._voice.last_activity_ts
            if idle > self.silence_timeout_s:
                log.info("session %s: %.0f s of silence, auto-closing", session_id, idle)
                await asyncio.to_thread(self._voice.stop)
                return

    async def _finish(self, session_id: uuid.UUID) -> None:
        try:
            await self._store.end(session_id)
        except Exception:
            log.exception("could not set ended_at for session %s", session_id)
        self._current = None
        await self._wait_mic_released()
        self._detector.reset()

    async def _wait_mic_released(self) -> None:
        for attempt in range(2):
            deadline = self._clock() + self._mic_release_timeout_s
            while self._voice.holds_microphone and self._clock() < deadline:
                await asyncio.sleep(0.02)
            if not self._voice.holds_microphone:
                return
            if attempt == 0:
                log.warning("voice session still holds the microphone; stopping it again")
                await asyncio.to_thread(self._voice.stop)
        log.error("voice session did not release the microphone; resuming anyway")

    async def _shutdown(self) -> None:
        self._set_state(State.STOPPED)
        # Record the end first: it is the cheap step, and the cost record must not depend on
        # the (slower) voice stop finishing.
        if self._current is not None:
            try:
                await self._store.end(self._current)
            except Exception:
                log.exception("could not set ended_at for session %s", self._current)
            self._current = None
        if self._start_task is not None and not self._start_task.done():
            await asyncio.wait({self._start_task}, timeout=15)
        if self._voice.active or self._voice.holds_microphone:
            try:
                await asyncio.to_thread(self._voice.stop)
            except Exception:
                log.exception("voice stop failed during shutdown")
        await self._audio.close()
