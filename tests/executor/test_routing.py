"""Unit tests for the task-difficulty router (TASK-46), against a fake OpenAI-compatible server.

Opt-in live tests (``RELAY_LLM_TESTS=1``) classify two real examples with the configured
router model (gemma4:e4b by default).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from relay.config import Settings
from relay.executor import routing
from relay.executor.routing import (
    RouteDecision,
    classify_difficulty,
    configure_classifier,
    parse,
    route_task,
    system_prompt,
    user_prompt,
)
from tests.conftest import REPO_ROOT


@dataclass
class FakeRouter:
    """Serves ``/v1/chat/completions`` with a canned reply; records every request body."""

    reply: str = '{"difficulty": "easy"}'
    status: int = 200
    delay_s: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    port: int = 0

    def settings(self, timeout_s: float = 2.0) -> Settings:
        return Settings(
            ollama_base_url=f"http://127.0.0.1:{self.port}/v1",
            router_model="ollama:gemma4:e4b",
            router_timeout_s=timeout_s,
        )


@pytest.fixture
def fake() -> Iterator[FakeRouter]:
    state = FakeRouter()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append({"path": self.path, **json.loads(body)})
            time.sleep(state.delay_s)
            payload = {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "gemma4:e4b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": state.reply},
                    }
                ],
            }
            data = json.dumps(payload if state.status == 200 else {"error": "boom"}).encode()
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client timed out and hung up

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


GOAL = "Research the latest open-weight LLMs that fit in 64 GB RAM on an M4 Pro"


def test_prompt_is_the_benchmark_v2_wording() -> None:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks" / "router"))
    try:
        bench = importlib.import_module("gemma_router")
        common = importlib.import_module("common")
    finally:
        sys.path.remove(str(REPO_ROOT / "benchmarks" / "router"))
    assert system_prompt() == bench.system_prompt("v2")
    item = {"goal": "g", "scope_excludes": ["no prices"]}
    assert routing.task_text("g", "no prices") == common.task_text(item)
    assert routing.task_text("g", "  ") == common.task_text({"goal": "g", "scope_excludes": []})


def test_request_shape_and_valid_answer(fake: FakeRouter) -> None:
    fake.reply = '{"difficulty": "easy"}'
    d = classify_difficulty(GOAL, "prices", fake.settings())
    assert (d.difficulty, d.valid, d.status, d.backend) == ("easy", True, "ok", "llm")
    assert d.raw == fake.reply and d.router_model == "ollama:gemma4:e4b"
    assert isinstance(d.latency_ms, int) and d.latency_ms >= 0
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions" and req["model"] == "gemma4:e4b"
    assert req["temperature"] == 0 and req["reasoning_effort"] == "none"
    assert req["response_format"] == {"type": "json_object"}
    assert req["messages"] == [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": f"task:\nGoal: {GOAL}\nExcluded from scope: prices"},
    ]
    assert user_prompt(GOAL, "prices") == req["messages"][1]["content"]


def test_hard_answer(fake: FakeRouter) -> None:
    fake.reply = '{"difficulty": " HARD "}'
    d = classify_difficulty(GOAL, "", fake.settings())
    assert (d.difficulty, d.valid, d.status) == ("hard", True, "ok")


@pytest.mark.parametrize(
    "reply",
    ['{"difficulty": "medium"}', "easy", '["easy"]', '{"label": "easy"}', ""],
)
def test_invalid_output_routes_hard(fake: FakeRouter, reply: str) -> None:
    fake.reply = reply
    d = classify_difficulty(GOAL, "", fake.settings())
    assert (d.difficulty, d.valid, d.status, d.raw) == ("hard", False, "invalid", reply)


def test_timeout_routes_hard_and_records_latency(fake: FakeRouter) -> None:
    fake.delay_s = 1.5
    d = classify_difficulty(GOAL, "", fake.settings(timeout_s=0.3))
    assert (d.difficulty, d.valid, d.status) == ("hard", False, "timeout")
    assert 250 <= d.latency_ms < 1400


def test_http_error_routes_hard(fake: FakeRouter) -> None:
    fake.status = 500
    d = classify_difficulty(GOAL, "", fake.settings())
    assert (d.difficulty, d.valid, d.status) == ("hard", False, "error")
    assert d.raw and "500" in d.raw


def test_unreachable_router_routes_hard() -> None:
    # router_model pinned: a developer .env may point it at a frontier model instead.
    s = Settings(
        ollama_base_url="http://127.0.0.1:9/v1",
        router_model="ollama:gemma4:e4b",
        router_timeout_s=0.5,
    )
    d = classify_difficulty(GOAL, "", s)
    assert (d.difficulty, d.valid) == ("hard", False)
    assert d.status in {"error", "timeout"}  # firewall policy decides refused vs. dropped


def test_unsupported_router_provider_routes_hard() -> None:
    d = classify_difficulty(GOAL, "", Settings(router_model="anthropic:claude-x"))
    assert (d.difficulty, d.valid, d.status) == ("hard", False, "error")


def test_parse() -> None:
    assert parse('{"difficulty": "easy"}') == "easy"
    assert parse(' {"difficulty":"Hard"} ') == "hard"
    assert parse(None) is None and parse("{") is None and parse('{"difficulty": 1}') is None


def test_route_task_uses_configured_classifier_and_fails_safe() -> None:
    s = Settings()

    def boom(goal: str, excludes: str, settings: Settings) -> RouteDecision:
        raise RuntimeError("router crashed")

    def easy(goal: str, excludes: str, settings: Settings) -> RouteDecision:
        return RouteDecision("easy", 1, True, "ok", "{}", settings.router_model)

    try:
        configure_classifier(easy)
        assert route_task(GOAL, "", s).difficulty == "easy"
        configure_classifier(boom)
        d = route_task(GOAL, "", s)
        assert (d.difficulty, d.valid, d.status) == ("hard", False, "error")
        assert d.raw and "router crashed" in d.raw
    finally:
        configure_classifier(None)


# --- opt-in live: the real router model ------------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="live LLM test: set RELAY_LLM_TESTS=1"
)


@live
@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        (
            "Research the latest open-weight LLMs that fit in 64 GB RAM on an M4 Pro, "
            "with quantization options",
            "hard",
        ),
        ("A short overview of what an SSH key is", "easy"),
    ],
)
def test_live_router_classifies_owner_examples(goal: str, expected: str) -> None:
    settings = Settings()
    # Warm the model first (a cold load can exceed the production timeout by design).
    classify_difficulty("warm-up", "", Settings(router_timeout_s=60))
    d = classify_difficulty(goal, "", settings)
    print(f"\nLIVE router {settings.router_model}: {d}")
    assert d.valid, d
    assert d.difficulty == expected
    assert d.latency_ms < settings.router_timeout_s * 1000
