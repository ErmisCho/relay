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
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import (
    AudioInterface,
    Conversation,
    ConversationInitiationData,
)

from relay.client.voice import EndCallback, TranscriptCallback
from relay.config import Settings, get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # the SDK contract: 16-bit PCM mono at 16 kHz, both directions
INPUT_BLOCK_FRAMES = 4_000  # 250 ms, the SDK's recommended input chunk
OUTPUT_BLOCK_FRAMES = 1_000  # 62.5 ms, bounds how long playback runs on after interrupt()
_BYTES_PER_FRAME = 2  # int16 mono
_INPUT_QUEUE_CHUNKS = 40  # 10 s of mic audio buffered while the socket is slow


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

    Output is a byte buffer drained by the output stream's callback, so `interrupt()` can drop
    everything queued at once: playback stops within one output block (62.5 ms).
    """

    def __init__(
        self,
        *,
        input_stream_factory: StreamFactory = _sounddevice_input_stream,
        output_stream_factory: StreamFactory = _sounddevice_output_stream,
    ) -> None:
        self._input_stream_factory = input_stream_factory
        self._output_stream_factory = output_stream_factory
        self._input_callback: Callable[[bytes], None] | None = None
        self._in_stream: AudioStream | None = None
        self._out_stream: AudioStream | None = None
        self._mic_open = False
        self._buffer = bytearray()
        self._lock = threading.Lock()  # guards _buffer; taken inside the output callback
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
        if in_stream is None and out_stream is None:
            return
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
        with self._lock:
            self._buffer.extend(audio)

    def interrupt(self) -> None:
        """Barge-in: drop all queued agent audio immediately."""
        with self._lock:
            self._buffer.clear()

    def _send_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                chunk = self._chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            callback = self._input_callback
            if callback is not None and not stop.is_set():
                callback(chunk)

    def _on_input(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._callback_threads.add(threading.get_ident())
        if self._input_callback is None:
            return
        try:
            self._chunks.put_nowait(bytes(indata))
        except queue.Full:
            pass  # the sender is stalled on the network; drop audio rather than block PortAudio

    def _on_output(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._callback_threads.add(threading.get_ident())
        wanted = frames * _BYTES_PER_FRAME
        with self._lock:
            chunk = bytes(self._buffer[:wanted])
            del self._buffer[:wanted]
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


def sdk_conversation_factory(settings: Settings) -> ConversationFactory:
    """Real SDK factory: a signed-URL (authenticated) websocket `Conversation`."""
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    client = ElevenLabs(api_key=settings.elevenlabs_api_key)

    def factory(**kwargs: Any) -> ConversationLike:
        return Conversation(client, requires_auth=True, **kwargs)

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
    ) -> None:
        self._settings = settings or get_settings()
        self._conversation_factory = conversation_factory
        self._audio_interface_factory = audio_interface_factory
        self.on_user_transcript: TranscriptCallback | None = None
        self.on_agent_response: TranscriptCallback | None = None
        self.on_end: EndCallback | None = None
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
        try:
            factory = self._conversation_factory or sdk_conversation_factory(self._settings)
            audio = self._audio_interface_factory()
            config = ConversationInitiationData(
                extra_body={"session_id": session_id},
                dynamic_variables={"session_id": session_id},
            )
            conversation = factory(
                agent_id=agent_id,
                audio_interface=audio,
                config=config,
                callback_user_transcript=lambda text: self._handle_user_transcript(token, text),
                callback_agent_response=lambda text: self._handle_agent_response(token, text),
                callback_end_session=lambda: self._finish(token),
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
            self._end_conversation(token, conversation)
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
        self._end_conversation(token, conversation)
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
            self._end_conversation(token, conversation)

    def _end_conversation(self, token: object, conversation: ConversationLike) -> None:
        # The SDK's end_session stops the audio interface, stops its client-tools loop and
        # calls callback_end_session (-> _finish). _finish runs again in case it did not.
        try:
            conversation.end_session()
        finally:
            self._finish(token)

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

    def _finish(self, token: object) -> None:
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
        try:
            if audio is not None:
                audio.stop()
        finally:
            callback = self.on_end
            if callback is not None:
                callback()
