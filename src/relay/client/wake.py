"""Client-side wake-word detection (SPEC §5).

Wake word is not native to ElevenLabs, so it runs locally on CPU. Everything above this module
talks to `WakeWordDetector`; openWakeWord is the default, and Porcupine (or any engine that
consumes 80 ms int16 frames) can be swapped in behind the same protocol.

openwakeword is imported lazily so the listener and its tests do not need it (or onnxruntime).
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz: openWakeWord's native hop
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE

Frame = npt.NDArray[np.int16]

MODEL_FIX = "uv sync --reinstall-package openwakeword"


@runtime_checkable
class WakeWordDetector(Protocol):
    """Consumes 16 kHz mono int16 frames of `FRAME_SAMPLES` and reports wake triggers."""

    @property
    def name(self) -> str:
        """The wake model name, recorded as `sessions.wake_trigger`."""
        ...

    def process(self, frame: Frame) -> bool:
        """Feed one frame; True exactly once per detected wake phrase."""
        ...

    def reset(self) -> None:
        """Forget buffered audio (call before resuming after the microphone was handed off)."""
        ...


class WakeModelMissingError(RuntimeError):
    """The requested openWakeWord model (or its feature models) is not on disk."""


class ScoringModel(Protocol):
    """The subset of `openwakeword.model.Model` this module uses."""

    def predict(self, x: Frame) -> dict[str, float]: ...

    def reset(self) -> None: ...


def resolve_model_path(wake_model: str) -> str:
    """Map a pretrained name (`hey_jarvis`) or an `.onnx` path to an existing model file.

    openwakeword 0.4.0 ships its pretrained models inside the wheel
    (`openwakeword/resources/models/*.onnx`); there is no download step.
    """
    if wake_model.endswith(".onnx"):
        path = wake_model
    else:
        try:
            import openwakeword  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise WakeModelMissingError(
                f"openwakeword is not installed; fix with `{MODEL_FIX}`"
            ) from exc
        known: dict[str, dict[str, str]] = openwakeword.models
        if wake_model not in known:
            raise WakeModelMissingError(
                f"unknown wake model {wake_model!r}; pretrained models: {sorted(known)} "
                "(or pass a path to a custom .onnx model)"
            )
        path = known[wake_model]["model_path"]
    if not os.path.isfile(path):
        raise WakeModelMissingError(f"wake model file {path} is missing; fix with `{MODEL_FIX}`")
    return path


def _openwakeword_model(wake_model: str) -> ScoringModel:
    path = resolve_model_path(wake_model)
    from openwakeword.model import Model  # type: ignore[import-untyped]

    try:
        # openwakeword 0.4.0 API: `wakeword_model_paths` (0.5+ renamed it `wakeword_models`).
        model: ScoringModel = Model(wakeword_model_paths=[path])
    except Exception as exc:  # onnxruntime raises its own types for missing feature models
        raise WakeModelMissingError(
            f"could not load openWakeWord model {path}: {exc}; fix with `{MODEL_FIX}`"
        ) from exc
    return model


class OpenWakeWordDetector:
    """`WakeWordDetector` on openWakeWord (ONNX, CPU).

    Triggers when the model score reaches `threshold`, then ignores scores for a refractory
    window measured in frames (so it behaves the same on live audio and faster-than-real-time
    WAV input). Frames keep flowing into the model during that window, which pushes the phrase
    out of its feature buffer before scoring resumes.
    """

    def __init__(
        self,
        wake_model: str,
        threshold: float = 0.5,
        *,
        refractory_s: float = 2.0,
        model_factory: Callable[[str], ScoringModel] = _openwakeword_model,
    ) -> None:
        self._name = wake_model
        self._model = model_factory(wake_model)
        # openWakeWord keys scores by the model file's basename (`hey_jarvis_v0.1`).
        self._score_key: str | None = None
        self.threshold = threshold
        self._refractory_frames = math.ceil(refractory_s / FRAME_S)
        self._cooldown = 0
        self.last_score = 0.0

    @property
    def name(self) -> str:
        return self._name

    def process(self, frame: Frame) -> bool:
        scores = self._model.predict(frame)
        if self._score_key is None:
            self._score_key = next(iter(scores))
        self.last_score = float(scores[self._score_key])
        if self._cooldown > 0:
            self._cooldown -= 1
            return False
        if self.last_score < self.threshold:
            return False
        self._cooldown = self._refractory_frames
        self._model.reset()  # clear the score buffer so the phrase is not re-scored
        return True

    def reset(self) -> None:
        # `Model.reset()` only clears scores; the audio feature buffer still holds the wake
        # phrase heard just before the hand-off. Flush it with silence so resuming cannot
        # re-trigger on stale audio.
        silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._refractory_frames):
            self._model.predict(silence)
        self._model.reset()
        self._cooldown = 0
        self.last_score = 0.0
