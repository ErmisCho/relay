"""Wake detector: debounce, missing-model error, and a real openWakeWord smoke test."""

from __future__ import annotations

import numpy as np
import pytest

from relay.client.wake import (
    FRAME_SAMPLES,
    MODEL_FIX,
    Frame,
    OpenWakeWordDetector,
    WakeModelMissingError,
    WakeWordDetector,
)


class ScriptedModel:
    """Returns `scores[i]` for the i-th predicted frame, like openwakeword's basename key."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.calls = 0
        self.resets = 0

    def predict(self, x: Frame) -> dict[str, float]:
        score = self.scores[self.calls] if self.calls < len(self.scores) else 0.0
        self.calls += 1
        return {"hey_jarvis_v0.1": score}

    def reset(self) -> None:
        self.resets += 1


SILENCE = np.zeros(FRAME_SAMPLES, dtype=np.int16)


def test_debounce_one_trigger_per_phrase_then_rearms() -> None:
    # A phrase scores high for several consecutive frames; it must open ONE session, not two.
    scores = [0.0] * 3 + [0.9] * 8 + [0.0] * 30 + [0.9]
    model = ScriptedModel(scores)
    det = OpenWakeWordDetector("hey_jarvis", 0.5, refractory_s=2.0, model_factory=lambda _: model)
    assert isinstance(det, WakeWordDetector)

    hits = [i for i in range(len(scores)) if det.process(SILENCE)]

    assert hits == [3, len(scores) - 1]  # 2 s = 25 frames of refractory, then re-armed
    assert model.resets == 2
    assert det.name == "hey_jarvis"


def test_unknown_model_names_the_choices() -> None:
    with pytest.raises(WakeModelMissingError, match="hey_jarvis"):
        OpenWakeWordDetector("hey_nobody")


def test_missing_model_file_names_the_fix(tmp_path: object) -> None:
    with pytest.raises(WakeModelMissingError, match=MODEL_FIX.split()[0]):
        OpenWakeWordDetector(f"{tmp_path}/absent.onnx")


def test_real_hey_jarvis_ignores_silence_and_noise() -> None:
    pytest.importorskip("openwakeword")
    try:
        det = OpenWakeWordDetector("hey_jarvis", 0.5)
    except WakeModelMissingError as exc:
        pytest.skip(str(exc))
    rng = np.random.default_rng(0)
    frames = [SILENCE] * 13 + [
        rng.normal(0, 300, FRAME_SAMPLES).astype(np.int16) for _ in range(13)
    ]
    max_score = 0.0
    for frame in frames:
        assert not det.process(frame)
        max_score = max(max_score, det.last_score)
    assert max_score < 0.05
