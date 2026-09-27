"""ElevenLabs Agents implementation of `VoiceSession` (SPEC §4, §5).

This is the ONLY module in `relay` that imports the ElevenLabs SDK (enforced by
`tests/client/test_sdk_isolation.py`). ElevenLabs owns STT, VAD, turn-taking, barge-in and
streaming TTS; relay never takes turn-taking back.

Transport: the installed Python SDK (`elevenlabs` 2.69.0) only offers the websocket transport
(`Conversation._run` opens `wss://.../v1/convai/conversation`); it has no WebRTC client, so this
module uses the websocket `Conversation`. Swapping to WebRTC later stays behind this seam.

The relay session id is sent twice in the `conversation_initiation_client_data` message:
as a dynamic variable (`dynamic_variables.session_id`) and in the custom-LLM extra body
(`custom_llm_extra_body.session_id`). With `platform_settings.overrides.custom_llm_extra_body`
enabled on the agent (see `config/elevenlabs/agent.json`), ElevenLabs forwards the latter to
the Delegator as `elevenlabs_extra_body.session_id`.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from typing import Any, Literal, Protocol

import numpy as np
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import (
    AudioInterface,
    Conversation,
    ConversationInitiationData,
)
from elevenlabs.version import __version__ as sdk_version
from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.client.voice import EndCallback, TranscriptCallback
from relay.config import Settings, get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # the SDK contract: 16-bit PCM mono at 16 kHz, both directions
INPUT_BLOCK_FRAMES = 4_000  # 250 ms, the SDK's recommended input chunk
OUTPUT_BLOCK_FRAMES = 1_000  # 62.5 ms, bounds how long playback runs on after interrupt()
_BYTES_PER_FRAME = 2  # int16 mono
_INPUT_QUEUE_CHUNKS = 40  # 10 s of mic audio buffered while the socket is slow

EchoGateMode = Literal["on", "off", "adaptive"]
ECHO_HANGOVER_S = 0.35  # Bluetooth output latency: echo keeps arriving after the last block
ECHO_MARGIN_DB = 10.0
PLAYBACK_LEVEL_WINDOW_S = 1.0  # how far back the playback-level estimate looks
PREBUFFER_S = 0.12  # buffer this much agent audio before playing after silence...
PREBUFFER_MAX_WAIT_S = 0.25  # ...or start anyway once the first chunk is this old
UNDERRUN_GAP_S = 0.5  # buffer ran dry and refilled within this: a gap, not an utterance end

VoiceGateMode = Literal["on", "off"]
VOICE_SUBFRAME_SAMPLES = 400  # 25 ms; divides the 4000-sample input chunk evenly
VOICE_MARGIN_DB = 12.0  # open when this far above the noise floor...
VOICE_MIN_DBFS = -40.0  # ...and at least this loud (a headset mic hears its wearer loudly)
VOICE_HOLD_S = 0.3  # stay open this long after the last qualifying sub-frame
VOICE_PREROLL_S = 0.075  # re-open this much audio before an onset (within the same chunk)
NOISE_FLOOR_MIN_DBFS = -75.0
NOISE_FLOOR_INITIAL_DBFS = -60.0
NOISE_FLOOR_RISE_DB_PER_S = 6.0  # through sub-frames below the open threshold
NOISE_FLOOR_ESCAPE_DB_PER_S = 2.0  # through qualifying sub-frames (loud steady room noise)
NOISE_FLOOR_FALL = 0.5  # fraction of the gap closed per quieter sub-frame
VOICE_CALIBRATION_LOG_S = 3.0


class AudioGateSettings(BaseSettings):
    """Mic gate settings from env/.env: `RELAY_ECHO_GATE` (on|off|adaptive),
    `RELAY_ECHO_GATE_MARGIN_DB`, `RELAY_VOICE_GATE` (on|off), `RELAY_VOICE_GATE_MARGIN_DB`,
    `RELAY_VOICE_GATE_MIN_DBFS`."""

    model_config = SettingsConfigDict(
        env_prefix="RELAY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    echo_gate: EchoGateMode = "adaptive"
    echo_gate_margin_db: float = ECHO_MARGIN_DB
    voice_gate: VoiceGateMode = "on"
    voice_gate_margin_db: float = VOICE_MARGIN_DB
    voice_gate_min_dbfs: float = VOICE_MIN_DBFS


def _rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def _dbfs(samples: np.ndarray[Any, Any]) -> float:
    if not samples.size:
        return -120.0
    x = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(x * x)))
    return 20.0 * math.log10(max(rms, 1e-3) / 32768.0)


class NearFieldVoiceGate:
    """Pass only the headset wearer's voice: it is centimetres from the mic, other people are
    metres away and typically 15-25 dB quieter.

    Each input chunk is judged in 25 ms sub-frames. A sub-frame qualifies when its level is at
    least `margin_db` above the adaptive noise floor AND at least `min_dbfs`. The gate opens
    on the first qualifying sub-frame (fast attack) and stays open `hold_s` after the last one
    so word endings and short pauses survive. Closed sub-frames become zeros; the chunk length
    never changes.

    Noise floor: falls quickly toward quieter sub-frames and rises at 6 dB/s through
    non-qualifying ones, so the wearer's speech (which qualifies) does not drag it up. Steady
    room noise loud enough to qualify would otherwise hold the gate open forever, so
    qualifying sub-frames still raise it, slowly (2 dB/s); speech has pauses between words
    that pull the floor straight back down. The floor never goes below -75 dBFS.

    Pre-roll: on an onset, up to `preroll_s` of the sub-frames just before it are re-opened,
    but only inside the current chunk. Earlier chunks have already been sent to the SDK;
    reaching back across the boundary would mean delaying the whole mic stream by the pre-roll
    (shifting its timing and adding latency to every turn), which this gate does not do.
    """

    def __init__(
        self,
        *,
        margin_db: float = VOICE_MARGIN_DB,
        min_dbfs: float = VOICE_MIN_DBFS,
        hold_s: float = VOICE_HOLD_S,
        preroll_s: float = VOICE_PREROLL_S,
    ) -> None:
        self.margin_db = margin_db
        self.min_dbfs = min_dbfs
        self._sub_s = VOICE_SUBFRAME_SAMPLES / SAMPLE_RATE
        self._hold_subframes = round(hold_s / self._sub_s)
        self._preroll_subframes = round(preroll_s / self._sub_s)
        self._hold_left = 0
        self.floor_dbfs = NOISE_FLOOR_INITIAL_DBFS
        self.voice_level_dbfs: float | None = None  # smoothed level of qualifying sub-frames
        self.elapsed_s = 0.0
        self.subframes = 0
        self.subframes_open = 0
        self._calibration_logged = False

    @property
    def threshold_dbfs(self) -> float:
        return max(self.floor_dbfs + self.margin_db, self.min_dbfs)

    def process(self, chunk: bytes, *, learn: bool = True) -> bytes:
        """Return `chunk` with closed sub-frames zeroed. `learn=False` freezes the floor (used
        while agent audio may be echoing into the mic)."""
        samples = np.frombuffer(chunk, dtype=np.int16)
        starts = range(0, samples.size, VOICE_SUBFRAME_SAMPLES)
        keep = [False] * len(starts)
        for i, start in enumerate(starts):
            sub = samples[start : start + VOICE_SUBFRAME_SAMPLES]
            dt = sub.size / SAMPLE_RATE
            level = _dbfs(sub)
            qualifies = level >= self.threshold_dbfs
            if qualifies:
                if self._hold_left == 0:  # onset: restore pre-roll inside this chunk
                    for j in range(max(0, i - self._preroll_subframes), i):
                        keep[j] = True
                keep[i] = True
                self._hold_left = self._hold_subframes
                self.voice_level_dbfs = (
                    level
                    if self.voice_level_dbfs is None
                    else 0.9 * self.voice_level_dbfs + 0.1 * level
                )
            elif self._hold_left > 0:
                keep[i] = True
                self._hold_left -= 1
            if learn:
                self._learn(level, qualifies, dt)
            self.elapsed_s += dt
        self.subframes += len(keep)
        self.subframes_open += sum(keep)
        self._maybe_log_calibration()
        if all(keep):
            return chunk
        out = samples.copy()
        for i, start in enumerate(starts):
            if not keep[i]:
                out[start : start + VOICE_SUBFRAME_SAMPLES] = 0
        return out.tobytes()

    def _learn(self, level: float, qualifies: bool, dt: float) -> None:
        if level < self.floor_dbfs:
            self.floor_dbfs += (level - self.floor_dbfs) * NOISE_FLOOR_FALL
        else:
            rate = NOISE_FLOOR_ESCAPE_DB_PER_S if qualifies else NOISE_FLOOR_RISE_DB_PER_S
            self.floor_dbfs = min(level, self.floor_dbfs + rate * dt)
        self.floor_dbfs = max(self.floor_dbfs, NOISE_FLOOR_MIN_DBFS)

    def _maybe_log_calibration(self) -> None:
        if self._calibration_logged or self.elapsed_s < VOICE_CALIBRATION_LOG_S:
            return
        self._calibration_logged = True
        voice = "n/a" if self.voice_level_dbfs is None else f"{self.voice_level_dbfs:.1f}"
        logger.info(
            "voice gate calibrated: noise floor %.1f dBFS, open threshold %.1f dBFS "
            "(margin %.1f dB, min %.1f dBFS), your voice so far %s dBFS",
            self.floor_dbfs,
            self.threshold_dbfs,
            self.margin_db,
            self.min_dbfs,
            voice,
        )


class AudioStream(Protocol):
    """The subset of a `sounddevice` stream this module uses."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


StreamFactory = Callable[..., AudioStream]


def _sounddevice_input_stream(**kwargs: Any) -> AudioStream:
    import sounddevice  # type: ignore[import-untyped]  # imported lazily: loads PortAudio

    stream: AudioStream = sounddevice.RawInputStream(**kwargs)
    return stream


def _sounddevice_output_stream(**kwargs: Any) -> AudioStream:
    import sounddevice

    stream: AudioStream = sounddevice.RawOutputStream(**kwargs)
    return stream


class SounddeviceAudioInterface(AudioInterface):
    """SDK `AudioInterface` on `sounddevice` (PyAudio, the SDK default, is not installed).

    Threading: the PortAudio callbacks only copy bytes. Microphone chunks go through a queue
    to a sender thread that calls the SDK's `input_callback` (a websocket send), so network
    I/O and the SDK's failure path (`end_session()` -> `stop()`) never run on the real-time
    audio thread. Stopping a PortAudio stream from inside its own callback never returns on
    CoreAudio, so `stop()` called on an audio callback thread hands the close to a helper.

    Echo gate (half duplex). There is no acoustic echo cancellation, so on a headset whose mic
    hears its own speaker (e.g. Bluetooth) the agent's voice reads to ElevenLabs as the user
    barging in, and replies get cut into ~1 s chunks. While agent audio is queued or playing,
    and for `hangover_s` after the last played block, mic chunks are replaced by silence of
    the same length (never dropped, so the SDK's audio clock stays intact). Modes:

    - `"on"`: strict. No echo, but also NO barge-in by voice while the agent speaks.
    - `"adaptive"` (default): a chunk passes during playback only if its RMS exceeds the
      recent playback level by `margin_db`, so a loud deliberate interruption still works.
      Too low a margin lets echo through again; too high makes barge-in need shouting.
    - `"off"`: stream the mic unchanged (use with hardware echo cancellation).

    Voice gate (`NearFieldVoiceGate`, on by default): mutes sub-frames that are not the
    wearer's near-field voice, so background speakers do not reach ElevenLabs' STT. A chunk
    is sent only as both gates allow: the echo gate decides first from the raw chunk (so a
    loud barge-in still works), then the voice gate zeroes what is not the wearer. The noise
    floor is frozen while the echo gate considers agent audio to be echoing.

    Output is a byte buffer drained by the output stream's callback. After silence, playback
    starts once `prebuffer_s` of audio is queued or the first chunk is `prebuffer_max_wait_s`
    old, to avoid gaps between network chunks. `interrupt()` drops everything queued at once:
    playback stops within one output block (62.5 ms).
    """

    def __init__(
        self,
        *,
        input_stream_factory: StreamFactory = _sounddevice_input_stream,
        output_stream_factory: StreamFactory = _sounddevice_output_stream,
        echo_gate: EchoGateMode | None = None,
        echo_margin_db: float | None = None,
        voice_gate: VoiceGateMode | None = None,
        voice_margin_db: float | None = None,
        voice_min_dbfs: float | None = None,
        hangover_s: float = ECHO_HANGOVER_S,
        prebuffer_s: float = PREBUFFER_S,
        prebuffer_max_wait_s: float = PREBUFFER_MAX_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if None in (echo_gate, echo_margin_db, voice_gate, voice_margin_db, voice_min_dbfs):
            env = AudioGateSettings()
            echo_gate = env.echo_gate if echo_gate is None else echo_gate
            echo_margin_db = env.echo_gate_margin_db if echo_margin_db is None else echo_margin_db
            voice_gate = env.voice_gate if voice_gate is None else voice_gate
            if voice_margin_db is None:
                voice_margin_db = env.voice_gate_margin_db
            if voice_min_dbfs is None:
                voice_min_dbfs = env.voice_gate_min_dbfs
        assert echo_gate is not None and echo_margin_db is not None
        assert voice_gate is not None and voice_margin_db is not None
        assert voice_min_dbfs is not None
        self.echo_gate: EchoGateMode = echo_gate
        self.voice_gate: NearFieldVoiceGate | None = (
            NearFieldVoiceGate(margin_db=voice_margin_db, min_dbfs=voice_min_dbfs)
            if voice_gate == "on"
            else None
        )
        self._margin_ratio = math.pow(10.0, echo_margin_db / 20.0)
        self._hangover_s = hangover_s
        self._prebuffer_bytes = int(prebuffer_s * SAMPLE_RATE) * _BYTES_PER_FRAME
        self._prebuffer_max_wait_s = prebuffer_max_wait_s
        self._clock = clock
        self._input_stream_factory = input_stream_factory
        self._output_stream_factory = output_stream_factory
        self._input_callback: Callable[[bytes], None] | None = None
        self._in_stream: AudioStream | None = None
        self._out_stream: AudioStream | None = None
        self._mic_open = False
        # Playback state, all guarded by _lock (taken inside the output callback).
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._playing = False
        self._first_pending_ts: float | None = None
        self._drained_ts: float | None = None
        self._last_played_ts: float | None = None
        self._played_levels: deque[tuple[float, float]] = deque(maxlen=64)
        # Per-session counters, logged at debug level on stop().
        self.stats: dict[str, float] = {
            "frames_in": 0,
            "frames_sent": 0,
            "frames_gated": 0,
            "frames_voice_gated": 0,
            "frames_dropped": 0,
            "underruns": 0,
            "interrupts": 0,
        }
        self._state_lock = threading.Lock()  # guards the stream/sender swap in start/stop
        self._callback_threads: set[int] = set()
        self._chunks: queue.Queue[bytes] = queue.Queue(maxsize=_INPUT_QUEUE_CHUNKS)
        self._sender: threading.Thread | None = None
        self._sender_stop = threading.Event()

    @property
    def holds_microphone(self) -> bool:
        return self._mic_open

    @property
    def pending_output_bytes(self) -> int:
        with self._lock:
            return len(self._buffer)

    def start(self, input_callback: Callable[[bytes], None]) -> None:
        with self._state_lock:
            self._input_callback = input_callback
            out_stream = self._output_stream_factory(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=OUTPUT_BLOCK_FRAMES,
                callback=self._on_output,
            )
            try:
                in_stream = self._input_stream_factory(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=INPUT_BLOCK_FRAMES,
                    callback=self._on_input,
                )
            except BaseException:
                # e.g. no input device: do not leak the already-open output stream.
                self._input_callback = None
                out_stream.close()
                raise
            self._out_stream = out_stream
            self._in_stream = in_stream
            self._mic_open = True
            self._sender_stop = threading.Event()
            self._sender = threading.Thread(
                target=self._send_loop,
                args=(self._sender_stop,),
                name="relay-voice-sender",
                daemon=True,
            )
            self._sender.start()
        out_stream.start()
        in_stream.start()

    def stop(self) -> None:
        """Close the microphone first (so the wake-word listener can reopen it), then output.

        Idempotent and safe to race: the SDK calls this from `end_session`, which can run
        more than once and concurrently with `ElevenLabsVoiceSession.stop()`.
        """
        with self._state_lock:
            self._input_callback = None
            in_stream, self._in_stream = self._in_stream, None
            out_stream, self._out_stream = self._out_stream, None
            sender, self._sender = self._sender, None
            self._sender_stop.set()
        with self._lock:
            self._buffer.clear()
            self._playing = False
        if in_stream is None and out_stream is None:
            return
        logger.debug(
            "voice audio stats (echo_gate=%s, voice_gate=%s): %s",
            self.echo_gate,
            "on" if self.voice_gate else "off",
            self.snapshot_stats(),
        )
        if threading.get_ident() in self._callback_threads:
            threading.Thread(
                target=self._close_streams,
                args=(in_stream, out_stream),
                name="relay-voice-close",
                daemon=True,
            ).start()
        else:
            self._close_streams(in_stream, out_stream)
        if sender is not None and sender is not threading.current_thread():
            sender.join(timeout=2)

    def _close_streams(self, in_stream: AudioStream | None, out_stream: AudioStream | None) -> None:
        for stream in (in_stream, out_stream):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:
                logger.exception("stopping audio stream failed")
            finally:
                try:
                    stream.close()
                except Exception:
                    logger.exception("closing audio stream failed")
                if stream is in_stream:
                    self._mic_open = False

    def output(self, audio: bytes) -> None:
        now = self._clock()
        with self._lock:
            if not self._buffer and not self._playing:
                self._first_pending_ts = now
                if self._drained_ts is not None and now - self._drained_ts < UNDERRUN_GAP_S:
                    self.stats["underruns"] += 1
                self._drained_ts = None
            self._buffer.extend(audio)

    def interrupt(self) -> None:
        """Barge-in: drop all queued agent audio immediately."""
        with self._lock:
            self._buffer.clear()
            self._playing = False
            self._first_pending_ts = None
            self._drained_ts = None
            self.stats["interrupts"] += 1

    def _send_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                chunk = self._chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            callback = self._input_callback
            if callback is not None and not stop.is_set():
                callback(chunk)
                self.stats["frames_sent"] += 1

    def snapshot_stats(self) -> dict[str, float]:
        """Counters plus the voice gate's current noise floor and open ratio."""
        stats = dict(self.stats)
        gate = self.voice_gate
        if gate is not None:
            stats["noise_floor_dbfs"] = round(gate.floor_dbfs, 1)
            stats["voice_threshold_dbfs"] = round(gate.threshold_dbfs, 1)
            stats["voice_open_ratio"] = (
                round(gate.subframes_open / gate.subframes, 3) if gate.subframes else 0.0
            )
            if gate.voice_level_dbfs is not None:
                stats["voice_level_dbfs"] = round(gate.voice_level_dbfs, 1)
        return stats

    def _echo_state(self) -> tuple[bool, float]:
        """(agent audio may be echoing into the mic, recent playback RMS)."""
        now = self._clock()
        with self._lock:
            echoing = bool(self._buffer) or (
                self._last_played_ts is not None and now - self._last_played_ts < self._hangover_s
            )
            playback_rms = max(
                (rms for ts, rms in self._played_levels if now - ts < PLAYBACK_LEVEL_WINDOW_S),
                default=0.0,
            )
        return echoing, playback_rms

    def _gate(self, chunk: bytes) -> bytes:
        """Apply the echo gate, then the voice gate. Same length out as in, always."""
        echoing, playback_rms = self._echo_state()
        echo_blocks = (
            self.echo_gate != "off"
            and echoing
            and not (
                self.echo_gate == "adaptive" and _rms(chunk) > playback_rms * self._margin_ratio
            )
        )
        voice_gate = self.voice_gate
        voiced = chunk
        if voice_gate is not None:
            # Always run it, so hold/floor state follows the real mic; never learn the floor
            # from agent echo.
            voiced = voice_gate.process(chunk, learn=not echoing)
        if echo_blocks:
            self.stats["frames_gated"] += 1
            return bytes(len(chunk))
        if voiced != chunk and not any(voiced):
            self.stats["frames_voice_gated"] += 1
        return voiced

    def _on_input(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._callback_threads.add(threading.get_ident())
        if self._input_callback is None:
            return
        self.stats["frames_in"] += 1
        try:
            self._chunks.put_nowait(self._gate(bytes(indata)))
        except queue.Full:
            # The sender is stalled on the network; drop rather than block PortAudio.
            self.stats["frames_dropped"] += 1

    def _on_output(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._callback_threads.add(threading.get_ident())
        wanted = frames * _BYTES_PER_FRAME
        now = self._clock()
        chunk = b""
        with self._lock:
            if not self._playing and self._buffer:
                first = self._first_pending_ts
                waited = 0.0 if first is None else now - first
                if (
                    len(self._buffer) >= self._prebuffer_bytes
                    or waited >= self._prebuffer_max_wait_s
                ):
                    self._playing = True
            if self._playing:
                chunk = bytes(self._buffer[:wanted])
                del self._buffer[:wanted]
                if chunk:
                    self._last_played_ts = now
                    self._played_levels.append((now, _rms(chunk)))
                if not self._buffer:
                    self._playing = False
                    self._drained_ts = now
        outdata[: len(chunk)] = chunk
        if len(chunk) < wanted:
            outdata[len(chunk) : wanted] = b"\x00" * (wanted - len(chunk))


class ConversationLike(Protocol):
    """The subset of the SDK `Conversation` this module drives."""

    def start_session(self) -> None: ...

    def end_session(self) -> None: ...

    def wait_for_session_end(self) -> str | None: ...


ConversationFactory = Callable[..., ConversationLike]
"""Called with the SDK `Conversation` keyword arguments minus `client`/`requires_auth`."""


# The signed-URL TTL is not stated anywhere in this repo or in the SDK source, so these ages are
# deliberately conservative. A URL older than MAX_AGE is never handed to a session, and a
# URL is refreshed in the background once it is REFRESH_AFTER old. Revisit both if
# ElevenLabs documents a shorter TTL, or make them settings (config.py is owned elsewhere).
SIGNED_URL_MAX_AGE_S = 300.0
SIGNED_URL_REFRESH_AFTER_S = 240.0


def _with_sdk_params(signed_url: str) -> str:
    """Append the query parameters the SDK's `Conversation._get_signed_url` adds.

    This mirrors conversation.py:517-527 in `elevenlabs` 2.69.0.
    """
    parsed = urllib.parse.urlparse(signed_url)
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params.extend([("source", "python_sdk"), ("version", sdk_version)])
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return urllib.parse.urlunparse(parsed._replace(query=query))


def sdk_signed_url_fetcher(settings: Settings) -> Callable[[], str]:
    """Real fetcher: one `get_signed_url` API call per call, returning a ready-to-dial URL."""
    agent_id = settings.elevenlabs_agent_id
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    if not agent_id:
        raise RuntimeError("ELEVENLABS_AGENT_ID is not set; run scripts/apply_agent_config.py")
    client = ElevenLabs(api_key=settings.elevenlabs_api_key)

    def fetch() -> str:
        response = client.conversational_ai.conversations.get_signed_url(agent_id=agent_id)
        return _with_sdk_params(response.signed_url)

    return fetch


class SignedUrlCache:
    """Keeps a fresh signed conversation URL ready while the wake listener idles (TASK-24 AC1).

    Without it, the `get_signed_url` API call ran only after the wake word, which put about
    half a second on trigger -> connected. `prefetch()` never blocks and never raises: a failed
    fetch logs once and leaves the cache empty, so `start()` falls back to the on-demand fetch.
    `take()` hands a URL out at most once (signed URLs are single-use).
    """

    def __init__(
        self,
        fetch: Callable[[], str],
        *,
        max_age_s: float = SIGNED_URL_MAX_AGE_S,
        refresh_after_s: float = SIGNED_URL_REFRESH_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._max_age_s = max_age_s
        self._refresh_after_s = refresh_after_s
        self._clock = clock
        self._lock = threading.Lock()
        self._url: str | None = None
        self._fetched_at = 0.0
        self._in_flight = False
        self._timer: threading.Timer | None = None
        self._failure_logged = False

    def prefetch(self) -> None:
        """Start a background fetch unless one is running or the cached URL is still young."""
        with self._lock:
            young = (
                self._url is not None
                and self._clock() - self._fetched_at < self._refresh_after_s
            )
            if self._in_flight or young:
                return
            self._in_flight = True
        threading.Thread(target=self._refresh, name="relay-signed-url", daemon=True).start()

    def refresh(self) -> None:
        """Fetch a URL now (blocking) and schedule the next refresh. Never raises."""
        with self._lock:
            self._in_flight = True
        self._refresh()

    def _refresh(self) -> None:
        try:
            url = self._fetch()
        except Exception as exc:
            with self._lock:
                self._in_flight = False
                first = not self._failure_logged
                self._failure_logged = True
            if first:
                logger.warning(
                    "signed URL prefetch failed (%s); the next session fetches it on demand", exc
                )
            return
        timer = threading.Timer(self._refresh_after_s, self.prefetch)
        timer.daemon = True
        with self._lock:
            self._url = url
            self._fetched_at = self._clock()
            self._in_flight = False
            self._failure_logged = False
            if self._timer is not None:
                self._timer.cancel()
            self._timer = timer
        timer.start()

    def take(self) -> str | None:
        """The cached URL if it is still within `max_age_s`, else None. Empties the cache."""
        with self._lock:
            url, self._url = self._url, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if url is not None and self._clock() - self._fetched_at < self._max_age_s:
                return url
        return None


class _PrefetchedUrlConversation(Conversation):
    """SDK `Conversation` that dials a prefetched signed URL instead of fetching one."""

    def __init__(self, *args: Any, signed_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prefetched_url = signed_url

    def _get_signed_url(self) -> str:
        url, self._prefetched_url = self._prefetched_url, None
        if url is None:
            # The SDK method carries no annotations (conversation.py:517).
            return str(super()._get_signed_url())  # type: ignore[no-untyped-call]
        return url


def sdk_conversation_factory(settings: Settings) -> ConversationFactory:
    """Real SDK factory: a signed-URL (authenticated) websocket `Conversation`.

    Takes an optional `signed_url` keyword; without it the SDK fetches one in `start_session()`.
    """
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    client = ElevenLabs(api_key=settings.elevenlabs_api_key)

    def factory(**kwargs: Any) -> ConversationLike:
        return _PrefetchedUrlConversation(client, requires_auth=True, **kwargs)

    return factory


class ElevenLabsVoiceSession:
    """`VoiceSession` backed by an ElevenLabs Agent whose LLM is the relay Delegator.

    Every start gets a private token; SDK callbacks and the watcher thread carry it, so a
    late event from a finished conversation can never end or touch a newer one.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        conversation_factory: ConversationFactory | None = None,
        audio_interface_factory: Callable[[], SounddeviceAudioInterface] = (
            SounddeviceAudioInterface
        ),
        signed_url_cache: SignedUrlCache | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._conversation_factory = conversation_factory
        self._audio_interface_factory = audio_interface_factory
        if signed_url_cache is None and conversation_factory is None:
            signed_url_cache = SignedUrlCache(self._fetch_signed_url)
        self._url_cache = signed_url_cache
        self._url_fetcher: Callable[[], str] | None = None
        self.on_user_transcript: TranscriptCallback | None = None
        self.on_agent_response: TranscriptCallback | None = None
        self.on_end: EndCallback | None = None
        # Why the last session ended ("stopped", "start_failed", or a VOICE_END_REASONS value);
        # set before `on_end` fires. `_ending` holds the reason of an end already under way.
        self.end_reason: str | None = None
        self._ending: str | None = None
        self._last_activity_ts = time.monotonic()
        self._lock = threading.Lock()
        self._token: object | None = None
        self._conversation: ConversationLike | None = None
        self._audio: SounddeviceAudioInterface | None = None
        self._session_id: str | None = None

    @property
    def last_activity_ts(self) -> float:
        return self._last_activity_ts

    @property
    def holds_microphone(self) -> bool:
        audio = self._audio
        return audio is not None and audio.holds_microphone

    @property
    def active(self) -> bool:
        return self._token is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _fetch_signed_url(self) -> str:
        if self._url_fetcher is None:
            self._url_fetcher = sdk_signed_url_fetcher(self._settings)
        return self._url_fetcher()

    def prepare(self) -> None:
        """Prefetch the signed URL in the background so the next `start()` skips that call."""
        if self._url_cache is not None:
            self._url_cache.prefetch()

    def start(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        agent_id = self._settings.elevenlabs_agent_id
        if not agent_id:
            raise RuntimeError("ELEVENLABS_AGENT_ID is not set; run scripts/apply_agent_config.py")
        token = object()
        with self._lock:
            if self._token is not None:
                raise RuntimeError("voice session already active")
            self._token = token
            self._ending = None
        try:
            factory = self._conversation_factory or sdk_conversation_factory(self._settings)
            audio = self._audio_interface_factory()
            config = ConversationInitiationData(
                extra_body={"session_id": session_id},
                dynamic_variables={"session_id": session_id},
            )
            signed_url = self._url_cache.take() if self._url_cache is not None else None
            url_kwargs = {} if signed_url is None else {"signed_url": signed_url}
            logger.info(
                "session %s: signed URL %s",
                session_id,
                "prefetched" if signed_url is not None else "fetched on demand",
            )
            conversation = factory(
                **url_kwargs,
                agent_id=agent_id,
                audio_interface=audio,
                config=config,
                callback_user_transcript=lambda text: self._handle_user_transcript(token, text),
                callback_agent_response=lambda text: self._handle_agent_response(token, text),
                callback_end_session=lambda: self._finish(token, "server_closed"),
            )
        except BaseException:
            with self._lock:
                self._token = None
            raise
        with self._lock:
            self._audio = audio
            self._conversation = conversation
            self._session_id = session_id
        self._touch()
        try:
            conversation.start_session()
        except BaseException:
            self._end_conversation(token, conversation, "start_failed")
            raise
        # The SDK runs the socket (and audio_interface.start) on its own thread with no
        # exception handling: a connect/handshake failure or a missing input device kills that
        # thread without end_session(). Watch it so every start still ends exactly once.
        threading.Thread(
            target=self._watch,
            args=(token, conversation),
            name="relay-voice-watch",
            daemon=True,
        ).start()

    def stop(self) -> None:
        with self._lock:
            token, conversation = self._token, self._conversation
        if token is None or conversation is None:
            return
        self._end_conversation(token, conversation, "stopped")
        try:
            conversation.wait_for_session_end()
        except RuntimeError:
            # Not started, or stop() called from the SDK's own thread (cannot join itself).
            pass

    def _watch(self, token: object, conversation: ConversationLike) -> None:
        try:
            conversation.wait_for_session_end()
        except RuntimeError:
            pass
        with self._lock:
            still_current = self._token is token
        if still_current:
            logger.warning("voice session thread ended without end_session; closing it")
            self._end_conversation(token, conversation, "connection_lost")

    def _end_conversation(
        self, token: object, conversation: ConversationLike, reason: str
    ) -> None:
        # The SDK's end_session stops the audio interface, stops its client-tools loop and
        # calls callback_end_session (-> _finish). _finish runs again in case it did not.
        # The reason is claimed first: that callback would otherwise report "server_closed".
        with self._lock:
            if self._token is token and self._ending is None:
                self._ending = reason
        try:
            conversation.end_session()
        finally:
            self._finish(token, reason)

    def _touch(self) -> None:
        self._last_activity_ts = time.monotonic()

    def _handle_user_transcript(self, token: object, text: str) -> None:
        if self._token is not token:
            return
        self._touch()
        callback = self.on_user_transcript
        if callback is not None:
            callback(text)

    def _handle_agent_response(self, token: object, text: str) -> None:
        if self._token is not token:
            return
        self._touch()
        callback = self.on_agent_response
        if callback is not None:
            callback(text)

    def _finish(self, token: object, reason: str) -> None:
        """Runs on every end path (stop, agent `end_call`, silence timeout, dropped socket,
        dead SDK thread). Only the first call for a given start releases the mic and fires
        `on_end`.
        """
        with self._lock:
            if self._token is not token:
                return
            audio = self._audio
            self._token = None
            self._conversation = None
            self._audio = None
            self.end_reason = self._ending or reason
            self._ending = None
        try:
            if audio is not None:
                audio.stop()
        finally:
            callback = self.on_end
            if callback is not None:
                callback()
