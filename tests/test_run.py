"""``uv run relay`` port pre-check (relay.run.port_owner)."""

from __future__ import annotations

import socket

import pytest

from relay import run


def test_port_owner_reports_a_live_listener_even_without_lsof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches, on Windows: SO_REUSEADDR binding over a live listener (conflict reported as
    # free, the Delegator then dies on startup), and the missing lsof crashing the check.
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert run.port_owner(port) == "another process"
    assert run.port_owner(port) is None
