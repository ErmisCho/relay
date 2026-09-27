"""Contract between relay's signed-URL prefetch (TASK-24) and the installed ElevenLabs SDK.

`_PrefetchedUrlConversation` overrides the SDK's private `Conversation._get_signed_url`, and
`_with_sdk_params` copies the query parameters that method appends. Neither is covered by the
SDK's public API, and pyproject pins only `elevenlabs>=2.69.0`, so an SDK upgrade can silently
skip the override (renamed method, or `start_session()` no longer calling it) or change the
appended params. These tests drive the REAL SDK `start_session()` -> `_run()` path with only the
websocket `connect` and the HTTP client replaced, so they fail on exactly those upgrades.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from relay.client.elevenlabs_session import _PrefetchedUrlConversation, _with_sdk_params


class _FakeWs:
    def __enter__(self) -> _FakeWs:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send(self, message: str) -> None:
        return None

    def recv(self, timeout: float | None = None) -> str:
        raise RuntimeError("fake socket closed")  # SDK `_run` answers with end_session()


class _SdkRig:
    """Records what the real SDK dials and how often it hits the signed-URL API."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, api_signed_url: str) -> None:
        self.dialed: list[str] = []
        self.api_calls: list[dict[str, Any]] = []

        def fake_connect(url: str, **kwargs: Any) -> _FakeWs:
            self.dialed.append(url)
            return _FakeWs()

        def fake_get_signed_url(**kwargs: Any) -> SimpleNamespace:
            self.api_calls.append(kwargs)
            return SimpleNamespace(signed_url=api_signed_url)

        monkeypatch.setattr("elevenlabs.conversational_ai.conversation.connect", fake_connect)
        self.client = SimpleNamespace(
            conversational_ai=SimpleNamespace(
                conversations=SimpleNamespace(get_signed_url=fake_get_signed_url)
            )
        )

    def run_session(self, signed_url: str | None) -> None:
        conversation = _PrefetchedUrlConversation(
            self.client, "agent_test", requires_auth=True, signed_url=signed_url
        )
        conversation.start_session()  # type: ignore[no-untyped-call]  # SDK method is unannotated
        conversation.wait_for_session_end()


def test_sdk_start_session_dials_the_prefetched_url_without_calling_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _SdkRig(monkeypatch, api_signed_url="wss://api/should-not-be-used?token=api")

    rig.run_session(signed_url="wss://prefetched/convai?token=pre&source=python_sdk")

    assert rig.dialed == ["wss://prefetched/convai?token=pre&source=python_sdk"]
    assert rig.api_calls == []


@pytest.mark.parametrize(
    "api_signed_url",
    [
        "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_test&conversation_signature=a%2Fb+c",
        "wss://api.elevenlabs.io/v1/convai/conversation",
    ],
)
def test_with_sdk_params_matches_the_url_the_sdk_builds_itself(
    monkeypatch: pytest.MonkeyPatch, api_signed_url: str
) -> None:
    # No prefetched URL: the override falls through to the SDK's own `_get_signed_url`, which
    # is the oracle for what the prefetch path must dial.
    rig = _SdkRig(monkeypatch, api_signed_url=api_signed_url)

    rig.run_session(signed_url=None)

    assert len(rig.api_calls) == 1
    assert rig.dialed == [_with_sdk_params(api_signed_url)]
