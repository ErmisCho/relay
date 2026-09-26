"""The swappable voice-layer seam (SPEC §5, §9).

The voice provider owns STT, VAD, turn-taking, barge-in and streaming TTS. Relay only starts
and stops a session and listens to its transcript events. Agent minutes are the dominant
running cost, so everything above this seam talks to `VoiceSession` and never to a vendor
SDK: a self-hosted STT/TTS pipeline can replace the ElevenLabs implementation later without
touching the wake-word loop or the auto-close watchdog.

This module deliberately imports no vendor SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

TranscriptCallback = Callable[[str], None]
EndCallback = Callable[[], None]

# Reasons an implementation may report through an optional `end_reason` attribute (read when
# `on_end` fires; not part of the protocol, so implementations without it stay conformant).
# Both are `sessions.end_reason` values: `server_closed` = the provider closed the call (the
# agent's `end_call` or a server-side limit), `connection_lost` = the connection died.
VOICE_END_REASONS = ("server_closed", "connection_lost")


@runtime_checkable
class VoiceSession(Protocol):
    """One live voice conversation bound to a relay session id.

    Integration contract (the wake-word loop and the auto-close watchdog rely on it):

    - Threads: `on_user_transcript`, `on_agent_response` and `on_end` are invoked on
      provider-owned background threads (SDK socket thread, audio sender thread, or a
      watcher thread), never on the caller's thread. Keep them short and thread-safe; from
      asyncio, hop back with `loop.call_soon_threadsafe(...)`.
    - Blocking: `start()` and `stop()` block (the ElevenLabs implementation makes an HTTP call
      for a signed URL in `start()` and joins the socket thread in `stop()`); from asyncio,
      call them via `asyncio.to_thread(...)`.
    - Microphone: `holds_microphone` becomes True only once the connection is up and the
      input stream is open (not when `start()` returns), and False once it is closed.
    - Ending: `on_end` fires exactly once per successful `start()`, whatever ended the
      session: `stop()`, the agent (`end_call`), a server-side silence timeout, a dropped
      connection, a failed connect, or a missing/failed audio device. The microphone has been
      released by the time `on_end` fires. If `start()` itself raises, `on_end` may also fire
      and the session is inactive afterwards, so it can be started again.
    """

    on_user_transcript: TranscriptCallback | None
    on_agent_response: TranscriptCallback | None
    on_end: EndCallback | None

    @property
    def last_activity_ts(self) -> float:
        """`time.monotonic()` of the last transcript/response event (or of `start`)."""
        ...

    @property
    def holds_microphone(self) -> bool:
        """True while the session has the microphone input stream open."""
        ...

    @property
    def active(self) -> bool:
        """True between a successful `start` and the end of the session."""
        ...

    def prepare(self) -> None:
        """Warm up for the next `start()` while the wake listener idles.

        Must not block and must not raise; a failed warm-up only makes `start()` slower.
        """
        ...

    def start(self, session_id: str) -> None:
        """Open the microphone and connect; `session_id` must reach the Delegator."""
        ...

    def stop(self) -> None:
        """End the session and release the microphone. Idempotent."""
        ...
