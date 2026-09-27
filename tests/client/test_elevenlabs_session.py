"""ElevenLabsVoiceSession wiring, offline: fake SDK Conversation and fake audio streams."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import numpy as np
import pytest
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation

from relay.client.elevenlabs_session import (
    ElevenLabsVoiceSession,
    SignedUrlCache,
    SounddeviceAudioInterface,
)
from relay.client.voice import VoiceSession
from relay.config import Settings
from tests.client.fakes import ConversationRecorder, StreamRecorder


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, elevenlabs_agent_id="agent_test")  # type: ignore[call-arg]


class Harness:
    def __init__(
        self,
        settings: Settings,
        *,
        conversation_factory: Any = None,
        inputs: StreamRecorder | None = None,
        signed_url_cache: SignedUrlCache | None = None,
    ) -> None:
        self.conversations = ConversationRecorder()
        self.inputs = inputs or StreamRecorder()
        self.outputs = StreamRecorder()
        self.ended = 0
        self.end_event = threading.Event()
        self.session = ElevenLabsVoiceSession(
            settings,
            conversation_factory=conversation_factory or self.conversations,
            audio_interface_factory=lambda: SounddeviceAudioInterface(
                input_stream_factory=self.inputs, output_stream_factory=self.outputs
            ),
            signed_url_cache=signed_url_cache,
        )
        self.session.on_end = self._on_end

    def _on_end(self) -> None:
        self.ended += 1
        self.end_event.set()

    def wait_for_end(self) -> None:
        assert self.end_event.wait(5), "on_end never fired"
        self.end_event.clear()
        time.sleep(0.05)  # let any duplicate end path run, so ended counts it


@pytest.fixture
def harness(settings: Settings) -> Harness:
    return Harness(settings)


def test_implements_voice_session_protocol(harness: Harness) -> None:
    assert isinstance(harness.session, VoiceSession)


def test_session_id_reaches_dynamic_variables_and_custom_llm_extra_body(
    harness: Harness,
) -> None:
    harness.session.start("sess-42")
    kwargs = dict(harness.conversations.conversations[0].kwargs)
    assert kwargs["agent_id"] == "agent_test"
    kwargs.pop("audio_interface")  # a real SDK Conversation would open it on start only

    # Build the REAL SDK Conversation with our kwargs and read the wire message it would send,
    # so a wrong SDK field name fails here rather than on a live call.
    conversation = Conversation(None, requires_auth=False, **kwargs)  # type: ignore[arg-type]
    try:
        message = json.loads(conversation._create_initiation_message())
    finally:
        conversation.client_tools.stop()
    assert message["type"] == "conversation_initiation_client_data"
    assert message["dynamic_variables"]["session_id"] == "sess-42"
    assert message["custom_llm_extra_body"]["session_id"] == "sess-42"


def test_transcript_and_response_callbacks_update_last_activity(harness: Harness) -> None:
    heard: list[str] = []
    harness.session.on_user_transcript = heard.append
    harness.session.on_agent_response = heard.append
    harness.session.start("s")
    conversation = harness.conversations.conversations[0]

    before = harness.session.last_activity_ts
    time.sleep(0.01)
    conversation.emit_user_transcript("hello")
    after_user = harness.session.last_activity_ts
    time.sleep(0.01)
    conversation.emit_agent_response("hi there")

    assert before < after_user < harness.session.last_activity_ts
    assert heard == ["hello", "hi there"]


def test_stop_releases_microphone_and_fires_on_end_once(harness: Harness) -> None:
    harness.session.start("s")
    assert harness.session.holds_microphone
    assert harness.inputs.streams[0].started

    harness.session.stop()
    harness.session.stop()

    assert harness.inputs.streams[0].close_calls == 1
    assert harness.outputs.streams[0].close_calls == 1
    assert not harness.session.holds_microphone
    assert not harness.session.active
    assert harness.ended == 1


def test_agent_end_call_releases_microphone_and_fires_on_end(harness: Harness) -> None:
    harness.session.start("s")
    conversation = harness.conversations.conversations[0]

    conversation.server_closed()
    conversation.server_closed()  # the SDK can report the end more than once

    assert harness.inputs.streams[0].close_calls == 1
    assert not harness.session.holds_microphone
    assert harness.ended == 1
    # The session can be restarted after the agent ended it.
    harness.session.start("s2")
    assert harness.session.holds_microphone


def test_interrupt_flushes_queued_playback() -> None:
    rig = AudioRig("on")
    rig.audio.output(b"\x01\x02" * 4000)
    assert rig.pull() != bytes(OUT_BLOCK_BYTES)  # playing
    assert rig.audio.pending_output_bytes == 8000 - OUT_BLOCK_BYTES

    rig.audio.interrupt()

    assert rig.audio.pending_output_bytes == 0
    # The next output block the device pulls is silence, not the rest of the sentence.
    assert rig.pull() == bytes(OUT_BLOCK_BYTES)
    assert rig.audio.stats["interrupts"] == 1


OUT_BLOCK_BYTES = 1_000 * 2
MIC_CHUNK_BYTES = 4_000 * 2


def _pcm(level: int, n_bytes: int) -> bytes:
    return np.full(n_bytes // 2, level, dtype=np.int16).tobytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class AudioRig:
    """A started SounddeviceAudioInterface on fake streams and a fake clock."""

    def __init__(self, echo_gate: Any, voice_gate: Any = "off") -> None:
        self.clock = FakeClock()
        self.outputs = StreamRecorder()
        self.sent: list[bytes] = []
        self.audio = SounddeviceAudioInterface(
            input_stream_factory=StreamRecorder(),
            output_stream_factory=self.outputs,
            echo_gate=echo_gate,
            echo_margin_db=10.0,
            voice_gate=voice_gate,
            voice_margin_db=12.0,
            voice_min_dbfs=-40.0,
            clock=self.clock,
        )
        self.audio.start(self.sent.append)

    def pull(self) -> bytes:
        """One output block, as PortAudio would request it."""
        frames = OUT_BLOCK_BYTES // 2
        block = bytearray(b"\xff" * OUT_BLOCK_BYTES)
        self.outputs.streams[0].kwargs["callback"](block, frames, None, None)
        return bytes(block)

    def mic(self, chunk: bytes) -> bytes:
        """Feed one mic chunk and return what the SDK received for it."""
        before = len(self.sent)
        self.audio._on_input(chunk, len(chunk) // 2, None, None)
        deadline = time.monotonic() + 2
        while len(self.sent) == before and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(self.sent) == before + 1, "mic chunk was dropped"
        return self.sent[-1]

    def run(self, signal: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        """Feed `signal` as 250 ms mic chunks; return what the SDK received, concatenated."""
        out = [
            np.frombuffer(self.mic(signal[i : i + 4_000].tobytes()), dtype=np.int16)
            for i in range(0, signal.size, 4_000)
        ]
        return np.concatenate(out)


def test_strict_gate_sends_silence_during_playback_and_hangover_without_dropping() -> None:
    rig = AudioRig("on")
    voice = _pcm(20_000, MIC_CHUNK_BYTES)
    silence = bytes(MIC_CHUNK_BYTES)

    rig.audio.output(_pcm(1_000, 2 * OUT_BLOCK_BYTES))
    assert rig.mic(voice) == silence  # queued agent audio
    rig.pull()
    rig.pull()  # drained at t=0
    rig.clock.now = 0.3
    assert rig.mic(voice) == silence  # hangover: Bluetooth echo still arriving
    rig.clock.now = 0.4
    assert rig.mic(voice) == voice

    assert rig.audio.stats["frames_gated"] == 2
    assert rig.audio.stats["frames_in"] == len(rig.sent) == 3


def test_gate_off_streams_the_mic_during_playback() -> None:
    rig = AudioRig("off")
    rig.audio.output(_pcm(1_000, 2 * OUT_BLOCK_BYTES))
    voice = _pcm(300, MIC_CHUNK_BYTES)
    assert rig.mic(voice) == voice


def test_adaptive_gate_passes_loud_barge_in_and_gates_quiet_echo() -> None:
    rig = AudioRig("adaptive")
    rig.audio.output(_pcm(1_000, 4 * OUT_BLOCK_BYTES))
    rig.pull()  # agent playing at RMS 1000; +10 dB threshold is ~3162

    echo = _pcm(500, MIC_CHUNK_BYTES)
    shout = _pcm(20_000, MIC_CHUNK_BYTES)
    assert rig.mic(echo) == bytes(MIC_CHUNK_BYTES)
    assert rig.mic(shout) == shout
    assert rig.audio.stats["frames_gated"] == 1
    assert rig.audio.stats["frames_in"] == len(rig.sent) == 2


def test_prebuffer_holds_playback_until_threshold_or_max_wait() -> None:
    rig = AudioRig("on")
    first = _pcm(1_000, OUT_BLOCK_BYTES)  # 62.5 ms < 120 ms pre-buffer
    rig.audio.output(first)
    assert rig.pull() == bytes(OUT_BLOCK_BYTES)
    assert rig.audio.pending_output_bytes == OUT_BLOCK_BYTES

    rig.audio.output(_pcm(1_000, OUT_BLOCK_BYTES))  # 125 ms buffered: start
    assert rig.pull() == first

    rig.pull()  # drain; next chunk arrives after silence and waits again
    rig.clock.now = 5.0
    tail = _pcm(2_000, 400)
    rig.audio.output(tail)
    assert rig.pull() == bytes(OUT_BLOCK_BYTES)
    rig.clock.now = 5.26  # older than the 250 ms max wait: play what we have
    assert rig.pull() == tail + bytes(OUT_BLOCK_BYTES - len(tail))


def _closed_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_real_sdk_connect_failure_ends_session_once_and_allows_restart(
    settings: Settings,
) -> None:
    # The real SDK Conversation, pointed at a closed local port: its socket thread dies in
    # connect() without calling end_session(). No ElevenLabs endpoint is contacted.
    client = ElevenLabs(api_key="offline-test", base_url=f"http://127.0.0.1:{_closed_local_port()}")

    def factory(**kwargs: Any) -> Conversation:
        return Conversation(client, requires_auth=False, **kwargs)

    harness = Harness(settings, conversation_factory=factory)
    harness.session.start("s1")
    harness.wait_for_end()
    assert harness.ended == 1
    assert not harness.session.active
    assert not harness.session.holds_microphone

    harness.session.start("s2")  # previously: "voice session already active"
    harness.wait_for_end()
    assert harness.ended == 2


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_missing_input_device_closes_output_stream_and_ends_once(settings: Settings) -> None:
    harness = Harness(settings, inputs=StreamRecorder(fail=OSError("no input device")))
    harness.conversations.threaded = True  # audio start runs on the SDK thread, as in `_run`

    harness.session.start("s")
    harness.wait_for_end()

    assert harness.ended == 1
    assert harness.outputs.streams[0].close_calls == 1
    assert not harness.session.active
    assert not harness.session.holds_microphone


def test_sdk_send_and_its_failure_path_run_off_the_audio_callback_thread() -> None:
    inputs = StreamRecorder()
    audio = SounddeviceAudioInterface(
        input_stream_factory=inputs, output_stream_factory=StreamRecorder()
    )
    sender_threads: list[threading.Thread] = []
    sent = threading.Event()

    def sdk_input_callback(chunk: bytes) -> None:
        # The SDK's reaction to a failed ws.send: end_session() -> audio_interface.stop().
        sender_threads.append(threading.current_thread())
        audio.stop()
        sent.set()

    audio.start(sdk_input_callback)
    portaudio = threading.Thread(target=audio._on_input, args=(b"\x01\x00" * 4, 4, None, None))
    portaudio.start()
    portaudio.join(2)

    assert sent.wait(2)
    assert sender_threads[0] is not portaudio
    stream = inputs.streams[0]
    assert stream.closed.wait(2)
    assert stream.stop_calls[0] is not portaudio
    assert not audio.holds_microphone


def test_stop_on_an_audio_callback_thread_is_handed_to_a_helper_and_closes_once() -> None:
    inputs = StreamRecorder()
    audio = SounddeviceAudioInterface(
        input_stream_factory=inputs, output_stream_factory=StreamRecorder()
    )
    audio.start(lambda _chunk: None)

    def portaudio_callback() -> None:
        audio._on_output(bytearray(8), 4, None, None)
        audio.stop()  # e.g. a callback that ends the session from the audio thread

    portaudio = threading.Thread(target=portaudio_callback)
    portaudio.start()
    portaudio.join(2)
    audio.stop()  # a racing second stop must not close twice

    stream = inputs.streams[0]
    assert stream.closed.wait(2)
    assert stream.stop_calls and all(t is not portaudio for t in stream.stop_calls)
    assert stream.close_calls == 1


# --- near-field voice gate -----------------------------------------------------------------

SR = 16_000


def _scaled(x: np.ndarray[Any, Any], dbfs: float) -> np.ndarray[Any, Any]:
    target = 32768.0 * 10 ** (dbfs / 20)
    x = x * (target / np.sqrt(np.mean(x * x)))
    return np.clip(x, -32768, 32767).astype(np.int16)


def noise(dbfs: float, seconds: float, seed: int = 1) -> np.ndarray[Any, Any]:
    return _scaled(np.random.default_rng(seed).standard_normal(int(seconds * SR)), dbfs)


def speech(dbfs: float, seconds: float, seed: int = 2) -> np.ndarray[Any, Any]:
    """Speech-like: noise with a 4 Hz syllable envelope, sustained at `dbfs` RMS."""
    n = int(seconds * SR)
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * np.arange(n) / SR)
    return _scaled(np.random.default_rng(seed).standard_normal(n) * envelope, dbfs)


def test_near_voice_passes_background_is_zeroed_and_no_frames_dropped() -> None:
    rig = AudioRig("off", voice_gate="on")
    room, voice = noise(-60, 2.0), speech(-20, 1.0)

    out = rig.run(np.concatenate([room, voice]))

    assert not out[: room.size].any()
    assert np.array_equal(out[room.size :], voice)
    assert rig.audio.stats["frames_in"] == len(rig.sent) == 12
    assert all(len(chunk) == 8_000 for chunk in rig.sent)


def test_distant_sustained_speech_is_gated() -> None:
    rig = AudioRig("off", voice_gate="on")

    out = rig.run(np.concatenate([noise(-60, 1.0), speech(-45, 3.0)]))

    assert not out.any()
    assert rig.audio.stats["frames_voice_gated"] == 16
    assert rig.audio.snapshot_stats()["voice_open_ratio"] == 0.0


def test_floor_adapts_to_a_louder_room_and_near_voice_still_passes() -> None:
    rig = AudioRig("off", voice_gate="on")
    loud_room, voice = noise(-45, 5.0, seed=3), speech(-20, 1.0)

    out = rig.run(np.concatenate([noise(-60, 2.0), loud_room, voice]))

    assert abs(rig.audio.snapshot_stats()["noise_floor_dbfs"] - (-45)) < 2
    assert not out[: -voice.size].any()
    assert np.array_equal(out[-voice.size :], voice)


def test_hold_keeps_a_200ms_pause_open_then_closes() -> None:
    rig = AudioRig("off", voice_gate="on")
    lead, pause, tail = noise(-60, 1.0), noise(-60, 0.2, seed=4), noise(-60, 1.0, seed=5)
    word1, word2 = speech(-20, 0.5), speech(-20, 0.5, seed=6)

    out = rig.run(np.concatenate([lead, word1, pause, word2, tail]))

    p0 = lead.size + word1.size
    assert np.array_equal(out[p0 : p0 + pause.size], pause)  # pause inside the utterance
    t0 = p0 + pause.size + word2.size
    assert np.array_equal(out[t0 : t0 + int(0.3 * SR)], tail[: int(0.3 * SR)])  # word ending
    assert not out[t0 + int(0.35 * SR) :].any()  # then the gate closes


def test_preroll_restores_the_onset_within_the_chunk() -> None:
    rig = AudioRig("off", voice_gate="on")
    rig.run(noise(-60, 1.0))
    before, onset = noise(-60, 2_000 / SR, seed=7), speech(-20, 2_000 / SR)

    out = rig.run(np.concatenate([before, onset]))

    assert not out[:800].any()
    assert np.array_equal(out[800:2_000], before[800:])  # 75 ms pre-roll
    assert np.array_equal(out[2_000:], onset)


def test_echo_and_voice_gates_combine() -> None:
    rig = AudioRig("adaptive", voice_gate="on")
    rig.audio.output(_pcm(1_000, 8 * OUT_BLOCK_BYTES))
    rig.pull()  # agent playing at ~-30 dBFS

    echo = speech(-35, 0.25)  # loud enough for the voice gate, but it is the agent's echo
    barge_in = speech(-6, 0.25)
    assert not rig.run(echo).any()
    assert np.array_equal(rig.run(barge_in), barge_in)

    rig.audio.interrupt()
    rig.clock.now = 1.0  # playback and hangover over
    rig.run(noise(-60, 0.5, seed=8))  # the barge-in's 300 ms hold runs out
    background, voice = noise(-60, 0.5), speech(-20, 0.5)
    assert not rig.run(background).any()
    assert np.array_equal(rig.run(voice), voice)
    assert rig.audio.stats["frames_gated"] == 1


def test_relay_voice_gate_off_streams_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_VOICE_GATE", "off")
    sent: list[bytes] = []
    audio = SounddeviceAudioInterface(
        input_stream_factory=StreamRecorder(),
        output_stream_factory=StreamRecorder(),
        echo_gate="off",
        echo_margin_db=10.0,
    )
    assert audio.voice_gate is None
    audio.start(sent.append)
    distant = speech(-45, 0.25).tobytes()
    audio._on_input(distant, 4_000, None, None)
    deadline = time.monotonic() + 2
    while not sent and time.monotonic() < deadline:
        time.sleep(0.005)
    audio.stop()
    assert sent == [distant]


class UrlFetcher:
    """Fake `get_signed_url`: counts calls, hands out distinct URLs, or raises `fail`."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return f"wss://signed/{self.calls}"


def _run_session(harness: Harness, session_id: str) -> dict[str, Any]:
    harness.session.start(session_id)
    kwargs = harness.conversations.conversations[-1].kwargs
    harness.session.stop()
    harness.wait_for_end()
    return kwargs


def test_fresh_prefetched_url_is_used_once_without_a_fetch_at_trigger(settings: Settings) -> None:
    # TASK-24 AC1: the wake path must not wait on get_signed_url when a fresh URL is ready,
    # and a signed URL is single-use, so the next session must not get it again.
    fetch = UrlFetcher()
    cache = SignedUrlCache(fetch, clock=FakeClock())
    cache.refresh()
    harness = Harness(settings, signed_url_cache=cache)

    first = _run_session(harness, "s1")
    second = _run_session(harness, "s2")

    assert first["signed_url"] == "wss://signed/1"
    assert fetch.calls == 1
    assert "signed_url" not in second  # used URL not reused -> SDK fetches on demand


def test_expired_prefetched_url_is_not_used(settings: Settings) -> None:
    clock = FakeClock()
    cache = SignedUrlCache(UrlFetcher(), max_age_s=300.0, clock=clock)
    cache.refresh()
    clock.now += 300.0
    harness = Harness(settings, signed_url_cache=cache)

    assert "signed_url" not in _run_session(harness, "s1")


def test_prefetch_failure_falls_back_to_on_demand_and_logs_once(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    fetch = UrlFetcher(fail=ConnectionError("offline"))
    cache = SignedUrlCache(fetch, clock=FakeClock())
    cache.refresh()  # must not raise into the listener
    cache.refresh()
    harness = Harness(settings, signed_url_cache=cache)

    assert "signed_url" not in _run_session(harness, "s1")
    assert fetch.calls == 2
    assert len([r for r in caplog.records if "prefetch failed" in r.getMessage()]) == 1


def test_prefetch_fetches_in_background_once_and_take_cancels_the_refresh_timer() -> None:
    # The listener calls prefetch() on every return to LISTENING: a young URL must not cost a
    # second get_signed_url, and a taken URL must not be refreshed by a leftover timer.
    fetch = UrlFetcher()
    cache = SignedUrlCache(fetch, refresh_after_s=0.2, clock=FakeClock())

    cache.prefetch()
    for t in [t for t in threading.enumerate() if t.name == "relay-signed-url"]:
        t.join(5)
    assert fetch.calls == 1

    cache.prefetch()  # clock has not moved: the cached URL is still young
    assert fetch.calls == 1

    assert cache.take() == "wss://signed/1"
    assert cache.take() is None
    time.sleep(0.4)  # past refresh_after_s: an uncancelled timer would prefetch again
    assert fetch.calls == 1
