"""ElevenLabsVoiceSession wiring, offline: fake SDK Conversation and fake audio streams."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation

from relay.client.elevenlabs_session import ElevenLabsVoiceSession, SounddeviceAudioInterface
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
    outputs = StreamRecorder()
    audio = SounddeviceAudioInterface(
        input_stream_factory=StreamRecorder(), output_stream_factory=outputs
    )
    audio.start(lambda _chunk: None)
    audio.output(b"\x01\x02" * 4000)
    assert audio.pending_output_bytes == 8000

    audio.interrupt()

    assert audio.pending_output_bytes == 0
    # The next output block the device pulls is silence, not the rest of the sentence.
    frames = outputs.streams[0].kwargs["blocksize"]
    block = bytearray(b"\xff" * frames * 2)
    outputs.streams[0].kwargs["callback"](block, frames, None, None)
    assert block == bytearray(frames * 2)


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
