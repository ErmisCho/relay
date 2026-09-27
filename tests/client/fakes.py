"""Offline stand-ins for the ElevenLabs SDK `Conversation` and `sounddevice` streams."""

from __future__ import annotations

import threading
from typing import Any


class FakeStream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = threading.Event()
        self.stop_calls: list[threading.Thread] = []
        self.close_calls = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stop_calls.append(threading.current_thread())

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class StreamRecorder:
    """A stream factory that remembers every stream it built (or raises `fail`)."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.streams: list[FakeStream] = []
        self.fail = fail

    def __call__(self, **kwargs: Any) -> FakeStream:
        if self.fail is not None:
            raise self.fail
        stream = FakeStream(**kwargs)
        self.streams.append(stream)
        return stream


class FakeConversation:
    """Mimics the SDK `Conversation` lifecycle (conversation.py:800-830) without a socket."""

    def __init__(self, *, threaded: bool = False, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.audio_interface = kwargs["audio_interface"]
        self.started = False
        self.threaded = threaded
        self._thread: threading.Thread | None = None
        self._ended = threading.Event()

    def start_session(self) -> None:
        self.started = True
        if not self.threaded:
            self.audio_interface.start(lambda _chunk: None)
            return

        # Like SDK `_run`: audio_interface.start runs on the SDK thread, unguarded, so an
        # exception there kills the thread without end_session().
        def run() -> None:
            self.audio_interface.start(lambda _chunk: None)
            self._ended.wait()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def end_session(self) -> None:
        # Same order as the SDK: stop audio, then fire callback_end_session.
        self.audio_interface.stop()
        self._ended.set()
        self.kwargs["callback_end_session"]()

    def wait_for_session_end(self) -> str | None:
        # Like the SDK, blocks until the (simulated) socket thread has finished.
        if self._thread is not None:
            self._thread.join()
        else:
            self._ended.wait()
        return None

    # Server-side events, as the SDK's message loop would deliver them.
    def emit_user_transcript(self, text: str) -> None:
        self.kwargs["callback_user_transcript"](text)

    def emit_agent_response(self, text: str) -> None:
        self.kwargs["callback_agent_response"](text)

    def server_closed(self) -> None:
        """Agent `end_call` closes the socket; the SDK reacts with `end_session()`."""
        self.end_session()


class ConversationRecorder:
    def __init__(self, *, threaded: bool = False) -> None:
        self.conversations: list[FakeConversation] = []
        self.threaded = threaded

    def __call__(self, **kwargs: Any) -> FakeConversation:
        conversation = FakeConversation(threaded=self.threaded, **kwargs)
        self.conversations.append(conversation)
        return conversation
