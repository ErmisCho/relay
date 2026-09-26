"""`uv run python -m relay.client`: wake word -> ElevenLabs voice session -> auto-close.

Every session started here spends ElevenLabs agent minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Coroutine
from typing import Any

from relay.client.capabilities import CapabilityPublishingStore
from relay.client.listener import (
    PostgresSessionStore,
    SounddeviceAudioSource,
    State,
    WakeListener,
)
from relay.client.wake import OpenWakeWordDetector
from relay.config import get_settings


def _device(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m relay.client", description=__doc__)
    parser.add_argument("--wake-model", default=settings.wake_model, help="default: WAKE_MODEL")
    parser.add_argument(
        "--threshold", type=float, default=settings.wake_threshold, help="default: WAKE_THRESHOLD"
    )
    parser.add_argument(
        "--silence-timeout",
        type=float,
        default=settings.silence_timeout_s,
        help="seconds without speech before auto-close (default: SILENCE_TIMEOUT_S)",
    )
    parser.add_argument(
        "--refractory",
        type=float,
        default=2.0,
        help="seconds to ignore the wake word after a trigger (default 2)",
    )
    parser.add_argument("--device", type=_device, help="input device index or name substring")
    parser.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    return parser.parse_args(argv)


async def run_until_signalled(main: Coroutine[Any, Any, None]) -> None:
    """Run `main`, cancelling it ONCE on SIGINT/SIGTERM and ignoring repeats.

    Under `uv run` one Ctrl-C reaches Python as two SIGINTs (the terminal's process group plus
    uv forwarding it). asyncio's default handler turns the second into a KeyboardInterrupt that
    aborts the listener's shutdown mid-way, leaving `sessions.ended_at` NULL.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(main)
    installed: list[signal.Signals] = []

    def request_stop(sig: signal.Signals) -> None:
        if task.cancelling():
            logging.getLogger(__name__).info("%s ignored: already shutting down", sig.name)
            return
        print("\nshutting down...", flush=True)
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # Windows / not the main thread
            pass
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _run(args: argparse.Namespace) -> None:
    from relay.client.elevenlabs_session import ElevenLabsVoiceSession
    from relay.store.db import create_engine, create_sessionmaker

    settings = get_settings()
    detector = OpenWakeWordDetector(args.wake_model, args.threshold, refractory_s=args.refractory)
    voice = ElevenLabsVoiceSession(settings)
    voice.on_user_transcript = lambda text: print(f"you:   {text}", flush=True)
    voice.on_agent_response = lambda text: print(f"agent: {text}", flush=True)
    engine = create_engine(settings.database_url)
    # Probes local capability now and on every wake, publishing it to the Delegator in the
    # background (TASK-35); it never delays or blocks a session.
    store = CapabilityPublishingStore(PostgresSessionStore(create_sessionmaker(engine)), settings)
    store.warm()
    listener = WakeListener(
        detector=detector,
        audio=SounddeviceAudioSource(args.device),
        voice=voice,
        store=store,
        silence_timeout_s=args.silence_timeout,
        on_state=lambda s: print(
            f"[{s.value}]" + (f" say {detector.name!r}" if s is State.LISTENING else ""),
            flush=True,
        ),
    )
    try:
        await run_until_signalled(listener.run())
    finally:
        await store.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.list_devices:
        import sounddevice  # type: ignore[import-untyped]

        print(sounddevice.query_devices())
        return
    if args.device is not None:
        # The ElevenLabs session opens the default input device, so point both at the choice.
        import sounddevice

        sounddevice.default.device = (args.device, sounddevice.default.device[1])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
