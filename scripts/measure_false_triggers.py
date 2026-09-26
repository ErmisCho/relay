"""Measure the wake-word false-trigger rate on background audio (TASK-24 DoD).

Runs the real openWakeWord detector with NO voice session (no agent minutes are spent) and
reports the trigger count, triggers/hour and the max score per minute.

    uv run python scripts/measure_false_triggers.py --minutes 60
    uv run python scripts/measure_false_triggers.py --wav background.wav   # 16 kHz mono int16
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
import wave
from collections.abc import AsyncIterator, Iterator

import numpy as np

from relay.client.listener import SounddeviceAudioSource
from relay.client.wake import FRAME_S, FRAME_SAMPLES, SAMPLE_RATE, Frame, OpenWakeWordDetector
from relay.config import get_settings


def wav_frames(path: str) -> Iterator[Frame]:
    with wave.open(path, "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise SystemExit(
                f"{path}: need 16 kHz mono 16-bit PCM; convert with "
                f"`ffmpeg -i {path} -ar 16000 -ac 1 -sample_fmt s16 out.wav`"
            )
        while len(data := wav.readframes(FRAME_SAMPLES)) == FRAME_SAMPLES * 2:
            yield np.frombuffer(data, dtype=np.int16)


async def mic_frames(device: int | str | None, minutes: float) -> AsyncIterator[Frame]:
    source = SounddeviceAudioSource(device)
    await source.open()
    deadline = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < deadline:
            yield await source.read()
    finally:
        await source.close()


class Tally:
    def __init__(self, detector: OpenWakeWordDetector) -> None:
        self.detector = detector
        self.frames = 0
        self.triggers: list[float] = []  # audio seconds
        self.minute_max: list[float] = []

    def feed(self, frame: Frame) -> None:
        audio_s = self.frames * FRAME_S
        minute = int(audio_s // 60)
        if minute == len(self.minute_max):
            if self.minute_max:
                print(f"minute {minute:3d}: max score {self.minute_max[-1]:.3f}", flush=True)
            self.minute_max.append(0.0)
        if self.detector.process(frame):
            self.triggers.append(audio_s)
            print(f"TRIGGER at {audio_s:8.1f} s (score {self.detector.last_score:.3f})", flush=True)
        self.minute_max[minute] = max(self.minute_max[minute], self.detector.last_score)
        self.frames += 1

    def report(self) -> None:
        hours = self.frames * FRAME_S / 3600
        rate = len(self.triggers) / hours if hours else 0.0
        print("\n=== false-trigger report ===")
        print(f"model:        {self.detector.name} (threshold {self.detector.threshold})")
        print(f"audio:        {self.frames * FRAME_S / 60:.1f} min")
        print(f"triggers:     {len(self.triggers)}")
        print(f"triggers/h:   {rate:.2f}")
        print("max score per minute: " + ", ".join(f"{m:.3f}" for m in self.minute_max))


async def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=60.0, help="mic duration (default 60)")
    parser.add_argument("--wav", help="measure a 16 kHz mono int16 WAV file instead of the mic")
    parser.add_argument("--wake-model", default=settings.wake_model)
    parser.add_argument("--threshold", type=float, default=settings.wake_threshold)
    parser.add_argument("--refractory", type=float, default=2.0)
    parser.add_argument("--device", type=lambda v: int(v) if v.isdigit() else v)
    args = parser.parse_args()

    tally = Tally(
        OpenWakeWordDetector(args.wake_model, args.threshold, refractory_s=args.refractory)
    )
    try:
        if args.wav:
            for frame in wav_frames(args.wav):
                tally.feed(frame)
        else:
            print(f"listening on the microphone for {args.minutes:g} min (Ctrl-C ends early)")
            async for frame in mic_frames(args.device, args.minutes):
                tally.feed(frame)
    finally:
        tally.report()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
