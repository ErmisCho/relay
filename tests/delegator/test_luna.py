"""gpt-6-luna as the Delegator's conversational model, with local gemma as the fallback (TASK-44).

Offline unit tests swap each model's HTTP transport for a scripted one (the OpenAI SDK runs on
``httpx2``, which respx does not patch). The live tests are opt-in (``RELAY_LLM_TESTS=1``;
they need ``OPENAI_API_KEY``, Ollama with gemma4:e4b and the compose Postgres) and run the
real app over a real socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings, get_settings
from relay.delegator import wiring
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolContext, ToolRegistry, ToolResult
from relay.delegator.llm import (
    ChatDelta,
    ChatModel,
    FallbackChatModel,
    OpenAICompatChatModel,
    build_chat_model,
    build_model,
)
from relay.delegator.llm.base import CONNECT_TIMEOUT_S, READ_TIMEOUT_S
from relay.store.db import create_engine, create_sessionmaker

from .conftest import AUTH, load_request, make_settings, parse_sse, post

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAI_HOST = "api.openai.com"
OLLAMA_HOST = "localhost"
LUNA = "openai:gpt-6-luna"
GEMMA = "ollama:gemma4:e4b"
MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "End the call.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    }
]

Reply = httpx2.Response | Exception | Callable[[httpx2.Request], Awaitable[httpx2.Response]]


class Route:
    """Scripted replies for one host; the last reply repeats once the script runs out."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = replies
        self.requests: list[httpx2.Request] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def sent(self, n: int = -1) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[n].content)
        return body


class Upstream:
    """Fake OpenAI + Ollama HTTP, installed into models via :meth:`wire`."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}

    def route(self, host: str, *replies: Reply) -> Route:
        self.routes[host] = Route(list(replies))
        return self.routes[host]

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        route = self.routes[request.url.host]
        route.requests.append(request)
        reply = route.replies.pop(0) if len(route.replies) > 1 else route.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return await reply(request)

    def wire(self, model: ChatModel) -> ChatModel:
        if isinstance(model, FallbackChatModel):
            self.wire(model.primary)
            self.wire(model.fallback)
        elif isinstance(model, OpenAICompatChatModel):
            transport = httpx2.MockTransport(self._handle)
            model._client = model._client.with_options(
                http_client=httpx2.AsyncClient(transport=transport)
            )
        return model


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture(autouse=True)
def _no_env_effort(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate from the developer's env/.env so the reasoning-effort default is observable."""
    monkeypatch.delenv("DELEGATOR_REASONING_EFFORT", raising=False)
    monkeypatch.chdir(tmp_path)


def luna_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "delegator_model": LUNA,
        "delegator_fallback_model": GEMMA,
        "openai_api_key": "sk-test",
        "ollama_base_url": "http://localhost:11434/v1",
    }
    values.update(overrides)
    return make_settings(**values)


def chunk(delta: dict[str, Any], finish: str | None = None, model: str = "gpt-6-luna") -> str:
    body = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


def sse(*chunks: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        content=("".join(chunks) + "data: [DONE]\n\n").encode(),
        headers={"content-type": "text/event-stream"},
    )


def text_stream(text: str, model: str = "gpt-6-luna") -> httpx2.Response:
    return sse(
        chunk({"role": "assistant", "content": ""}, model=model),
        chunk({"content": text}, model=model),
        chunk({}, "stop", model=model),
    )


def luna_tool_stream(name: str, fragments: list[str], call_id: str = "call_abc") -> httpx2.Response:
    """The fragment shape gpt-6-luna streamed live: id+name with empty arguments, then pieces."""
    first = {"index": 0, "id": call_id, "type": "function"}
    first["function"] = {"name": name, "arguments": ""}
    return sse(
        chunk({"role": "assistant", "content": None, "tool_calls": [first]}),
        *(chunk({"tool_calls": [{"index": 0, "function": {"arguments": f}}]}) for f in fragments),
        chunk({}, "tool_calls"),
    )


def error(status: int, **err: Any) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": "x", **err}})


def unsupported(param: str, code: str = "unsupported_value") -> httpx2.Response:
    return error(400, type="invalid_request_error", param=param, code=code)


async def collect(model: ChatModel, **kwargs: Any) -> list[ChatDelta]:
    tools = kwargs.pop("tools", None)
    return [d async for d in model.stream(MESSAGES, tools, **kwargs)]


def contents(deltas: list[ChatDelta]) -> list[str]:
    return [d.content for d in deltas if d.content]


# --- request params -----------------------------------------------------------------------


async def test_openai_ref_sends_reasoning_none_and_max_completion_tokens(
    upstream: Upstream,
) -> None:
    route = upstream.route(OPENAI_HOST, text_stream("Hi."))
    model = upstream.wire(build_model(LUNA, luna_settings()))
    await collect(model, tools=TOOLS, temperature=0.5, max_tokens=300, tool_choice="auto")

    body = route.sent()
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "none"
    assert body["max_completion_tokens"] == 300 and "max_tokens" not in body
    # A non-default temperature is accepted only with reasoning_effort="none".
    assert body["temperature"] == 0.5
    assert body["tool_choice"] == "auto" and body["tools"] == TOOLS
    assert "parallel_tool_calls" not in body
    assert route.requests[0].headers["authorization"] == "Bearer sk-test"


async def test_ollama_ref_keeps_its_params(upstream: Upstream) -> None:
    route = upstream.route(OLLAMA_HOST, text_stream("Hi.", model="gemma4:e4b"))
    model = upstream.wire(build_model(GEMMA, luna_settings()))
    await collect(model, temperature=0.5, max_tokens=300)

    body = route.sent()
    assert body["model"] == "gemma4:e4b"
    assert body["reasoning_effort"] == "none"
    assert body["max_tokens"] == 300 and "max_completion_tokens" not in body
    assert body["temperature"] == 0.5
    assert "tools" not in body and "tool_choice" not in body


async def test_tool_choice_is_never_sent_without_tools(upstream: Upstream) -> None:
    route = upstream.route(OPENAI_HOST, text_stream("Hi."))
    await collect(upstream.wire(build_model(LUNA, luna_settings())), tool_choice="none")
    assert "tool_choice" not in route.sent()


async def test_delegator_reasoning_effort_env_is_honoured(
    upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DELEGATOR_REASONING_EFFORT", "low")
    route = upstream.route(OPENAI_HOST, text_stream("Hi."))
    model = build_chat_model(luna_settings())
    assert isinstance(model, FallbackChatModel)
    await collect(upstream.wire(model.primary), temperature=0.5, max_tokens=300)

    body = route.sent()
    assert body["reasoning_effort"] == "low"
    # gpt-6-luna rejects a non-default temperature once reasoning is on.
    assert "temperature" not in body


async def test_reasoning_effort_default_omits_the_param(
    upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DELEGATOR_REASONING_EFFORT", "default")
    route = upstream.route(OPENAI_HOST, text_stream("Hi."))
    model = build_chat_model(luna_settings())
    assert isinstance(model, FallbackChatModel)
    await collect(upstream.wire(model.primary), temperature=0.5)
    assert "reasoning_effort" not in route.sent() and "temperature" not in route.sent()


def test_openai_client_uses_voice_timeouts_without_sdk_retries() -> None:
    model = build_model(LUNA, luna_settings())
    assert isinstance(model, OpenAICompatChatModel)
    client = model._client
    assert client.max_retries == 0
    timeout = client.timeout
    assert not isinstance(timeout, float | None)
    assert timeout.connect == CONNECT_TIMEOUT_S
    assert timeout.read == READ_TIMEOUT_S


def test_classifier_models_never_follow_delegator_model() -> None:
    # Assent/ready/summary are built from their own refs (commitment.protocol and
    # hooks.ideas call build_model(settings.<x>_model)); DELEGATOR_MODEL doesn't leak in.
    settings = luna_settings()
    for ref in (settings.assent_model, settings.ready_model, settings.summary_model):
        assert build_model(ref, settings).model_name == GEMMA


def test_startup_log_names_models_and_params(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="relay.delegator.llm.factory")
    build_chat_model(luna_settings())
    (line,) = [r.getMessage() for r in caplog.records]
    assert "openai:gpt-6-luna (reasoning_effort=none" in line
    assert "max_completion_tokens" in line
    assert f"fallback: {GEMMA}" in line


async def test_rejected_optional_param_is_dropped_not_failed_over(
    upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    route = upstream.route(
        OPENAI_HOST, unsupported("temperature"), text_stream("Hi."), text_stream("Again.")
    )
    model = upstream.wire(build_model(LUNA, luna_settings()))
    first = await collect(model, temperature=0.5, max_tokens=50)
    second = await collect(model, temperature=0.5, max_tokens=50)

    assert contents(first) == ["Hi."] and contents(second) == ["Again."]
    assert route.call_count == 3  # one rejected request, then no more 400s
    assert "temperature" in route.sent(0)
    assert "temperature" not in route.sent(1) and "temperature" not in route.sent(2)
    assert sum("rejected temperature" in r.getMessage() for r in caplog.records) == 1


async def test_rejected_max_tokens_is_renamed(upstream: Upstream) -> None:
    route = upstream.route(
        OLLAMA_HOST, unsupported("max_tokens", "unsupported_parameter"), text_stream("Hi.")
    )
    await collect(upstream.wire(build_model(GEMMA, luna_settings())), max_tokens=50)
    assert route.sent(1)["max_completion_tokens"] == 50 and "max_tokens" not in route.sent(1)


async def test_other_bad_requests_still_raise(upstream: Upstream) -> None:
    upstream.route(OPENAI_HOST, error(400, param="messages", code="invalid_value"))
    with pytest.raises(Exception, match="400"):
        await collect(upstream.wire(build_model(LUNA, luna_settings())))


# --- fallback -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        error(500),
        error(401, code="invalid_api_key"),
        error(429),
        httpx2.ReadTimeout("read timed out"),
        httpx2.ConnectTimeout("connect timed out"),
        httpx2.ConnectError("no route"),
    ],
    ids=["500", "auth", "429", "read-timeout", "connect-timeout", "connect-error"],
)
async def test_openai_failure_falls_back_to_gemma(upstream: Upstream, failure: Reply) -> None:
    luna = upstream.route(OPENAI_HOST, failure)
    gemma = upstream.route(OLLAMA_HOST, text_stream("Local.", model="gemma4:e4b"))
    model = upstream.wire(build_chat_model(luna_settings()))

    deltas = await collect(model, temperature=0.5, max_tokens=300)

    assert luna.call_count == 1  # max_retries=0: no SDK backoff before the switch
    assert contents(deltas) == ["Local."]
    assert {d.model for d in deltas} == {GEMMA}
    assert gemma.sent()["max_tokens"] == 300


async def test_missed_first_delta_deadline_falls_back(upstream: Upstream) -> None:
    async def hang(_: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)
        return text_stream("too late")

    upstream.route(OPENAI_HOST, hang)
    upstream.route(OLLAMA_HOST, text_stream("Local.", model="gemma4:e4b"))
    settings = luna_settings()
    model = FallbackChatModel(
        upstream.wire(build_model(LUNA, settings)),
        upstream.wire(build_model(GEMMA, settings)),
        first_delta_timeout=0.2,
    )
    started = time.perf_counter()
    deltas = await collect(model)
    assert time.perf_counter() - started < 5
    assert contents(deltas) == ["Local."]
    assert {d.model for d in deltas} == {GEMMA}


def test_luna_to_gemma_uses_the_short_deadline() -> None:
    model = build_chat_model(luna_settings())
    assert isinstance(model, FallbackChatModel)
    assert model.first_delta_timeout == 5.0


async def test_fallback_is_silent_and_recorded_through_the_delegator(
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    upstream.route(OPENAI_HOST, error(503))
    upstream.route(OLLAMA_HOST, text_stream("Tell me more.", model="gemma4:e4b"))
    settings = luna_settings()
    model = upstream.wire(build_chat_model(settings))
    app = create_app(
        settings, chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    caplog.set_level(logging.INFO, logger="relay.delegator.service")

    chunks = parse_sse((await post(app, load_request())).content)

    reply = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert reply == "Tell me more."  # no apology, no error text
    (record,) = [r for r in caplog.records if r.getMessage().startswith("delegator turn")]
    assert isinstance(record.args, tuple) and record.args[2] == GEMMA  # model_used


# --- streamed tool calls ------------------------------------------------------------------


async def test_luna_tool_call_fragments_are_assembled_for_system_tools(
    upstream: Upstream, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    upstream.route(
        OPENAI_HOST, luna_tool_stream("end_call", ['{"', "reason", '":"', "user said bye", '"}'])
    )
    settings = luna_settings()
    model = upstream.wire(build_chat_model(settings))
    app = create_app(
        settings, chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )

    chunks = parse_sse((await post(app, load_request())).content)

    calls = [tc for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert calls == [
        {
            "index": 0,
            "id": "call_abc",
            "type": "function",
            "function": {"name": "end_call", "arguments": '{"reason":"user said bye"}'},
        }
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


class RecordingTool:
    name = "secret_lookup"
    description = "Look something up."
    parameters: dict[str, Any] = {"type": "object", "properties": {"q": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return ToolResult(content="TOPSECRET-42")


async def test_luna_internal_tool_call_runs_server_side(
    upstream: Upstream, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    route = upstream.route(
        OPENAI_HOST,
        luna_tool_stream("secret_lookup", ['{"q"', ':"bike', ' lock"}'], call_id="call_1"),
        text_stream("Found it."),
    )
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    settings = luna_settings()
    model = upstream.wire(build_chat_model(settings))
    app = create_app(settings, chat_model=model, registry=registry, hooks=[], sessionmaker=dead_db)

    chunks = parse_sse((await post(app, load_request())).content)

    assert tool.calls == [{"q": "bike lock"}]
    reply = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert reply == "Found it."
    assert not any(c["choices"][0]["delta"].get("tool_calls") for c in chunks)
    # Round two carries the assistant tool call and its result back to gpt-6-luna.
    followup = route.sent(1)["messages"]
    assert followup[-2]["tool_calls"][0]["id"] == "call_1"
    assert followup[-2]["tool_calls"][0]["function"]["arguments"] == '{"q":"bike lock"}'
    assert followup[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "TOPSECRET-42"}


# --- live (opt-in) ------------------------------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="set RELAY_LLM_TESTS=1 to hit live models"
)

TTFT_PROMPTS = [
    "What's a good name for a bike lock startup?",
    "Quick question, is Rust hard to learn?",
    "Should I use Postgres or SQLite for a side project?",
    "Give me one tip for better sleep.",
    "What do you think of wake word detection on a laptop?",
    "How do I stay focused while working from home?",
    "Is a phone-proximity bike lock a good idea?",
    "What's the difference between RAM and VRAM?",
    "Tell me something interesting about sourdough.",
    "How long should a standup meeting be?",
    "What's quantization in one sentence?",
    "Any thoughts on learning piano as an adult?",
]
IN_SCOPE = [
    "I'm just wondering how to run local LLMs on my Mac Mini M4 Pro with 64 GB RAM, "
    "what are your opinions?",
    "Can you research the latest models that fit in that RAM?",
]
OUT_OF_SCOPE = ["Send an email to my boss", "Book a table", "Post this on Slack"]
REFUSAL_MARKERS = ("can't do that", "cannot do that", "can't send", "can't book", "can't post")


class SpyTool:
    """Wraps an internal tool, recording each call's arguments."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        result: ToolResult = await self.inner(args, ctx)
        return result


def spied_registry() -> tuple[ToolRegistry, SpyTool]:
    registry = ToolRegistry()
    spy: SpyTool | None = None
    for tool in wiring.build_registry():
        if tool.name == "propose_commitment":
            spy = SpyTool(tool)
            registry.register(spy)
        else:
            registry.register(tool)
    assert spy is not None
    return registry, spy


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
async def live_server(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[str, SpyTool]]:
    import uvicorn

    monkeypatch.chdir(REPO_ROOT)  # the real .env (OPENAI_API_KEY, DELEGATOR_REASONING_EFFORT)
    key = get_settings().openai_api_key
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    settings = make_settings(
        delegator_model=LUNA,
        delegator_fallback_model=GEMMA,
        openai_api_key=key,
        database_url=migrated_db_url,
    )
    engine = create_engine(migrated_db_url)
    registry, spy = spied_registry()
    app = create_app(
        settings,
        registry=registry,
        hooks=wiring.build_hooks(),
        sessionmaker=create_sessionmaker(engine),
        warm_dbos=False,
    )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}", spy
    server.should_exit = True
    await task
    await engine.dispose()


def body_for(messages: list[dict[str, str]], session_id: uuid.UUID) -> dict[str, Any]:
    body = load_request()
    body["messages"] = [body["messages"][0], *messages]
    body["elevenlabs_extra_body"]["session_id"] = str(session_id)
    return body


async def converse(
    client: httpx.AsyncClient, url: str, body: dict[str, Any]
) -> tuple[float | None, str]:
    """POST over the socket; returns (seconds to the first content token, full reply)."""
    started = time.perf_counter()
    ttft: float | None = None
    parts: list[str] = []
    async with client.stream("POST", f"{url}/v1/chat/completions", json=body, headers=AUTH) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            content = json.loads(line[6:])["choices"][0]["delta"].get("content")
            if content:
                ttft = ttft if ttft is not None else time.perf_counter() - started
                parts.append(content)
    return ttft, "".join(parts)


def turn_models(caplog: pytest.LogCaptureFixture) -> list[str]:
    records = [r for r in caplog.records if r.getMessage().startswith("delegator turn")]
    return [str(r.args[2]) for r in records]  # type: ignore[index]


@live
async def test_live_luna_ttft_over_a_real_socket(
    live_server: tuple[str, SpyTool], caplog: pytest.LogCaptureFixture
) -> None:
    url, _ = live_server
    caplog.set_level(logging.INFO, logger="relay.delegator.service")
    ttfts: list[float] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for prompt in TTFT_PROMPTS:
            body = body_for([{"role": "user", "content": prompt}], uuid.uuid4())
            ttft, reply = await converse(client, url, body)
            assert ttft is not None and reply.strip()
            ttfts.append(ttft)
            print(f"\n  ttft={ttft * 1000:6.0f} ms  {prompt[:40]!r} -> {reply[:70]!r}")
    await asyncio.sleep(0.5)  # turn finalisation (the log line) runs detached
    models = turn_models(caplog)
    ms = sorted(round(t * 1000) for t in ttfts)
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    print(f"\nTTFT ms over {len(ms)} turns: {ms}; median={statistics.median(ms)} p95={p95}")
    print(f"model_used per turn: {models}")
    assert len(ttfts) >= 10


@live
async def test_live_luna_scope(
    live_server: tuple[str, SpyTool], caplog: pytest.LogCaptureFixture
) -> None:
    url, spy = live_server
    caplog.set_level(logging.INFO, logger="relay.delegator.service")
    problems: list[str] = []
    async with httpx.AsyncClient(timeout=60) as client:
        # The owner's two live phrasings, as one conversation.
        session = uuid.uuid4()
        history: list[dict[str, str]] = []
        for utterance in IN_SCOPE:
            history.append({"role": "user", "content": utterance})
            _, reply = await converse(client, url, body_for(history, session))
            history.append({"role": "assistant", "content": reply})
            refused = any(m in reply.lower().replace("’", "'") for m in REFUSAL_MARKERS)
            print(f"\n[{'REFUSED' if refused else 'OK'}] {utterance[:50]!r}\n    {reply[:200]!r}")
            if refused or not reply.strip():
                problems.append(utterance)
        print(f"  propose_commitment calls: {spy.calls}")
        for utterance in OUT_OF_SCOPE:
            _, reply = await converse(
                client, url, body_for([{"role": "user", "content": utterance}], uuid.uuid4())
            )
            refused = any(m in reply.lower().replace("’", "'") for m in REFUSAL_MARKERS)
            print(
                f"\n[{'refused' if refused else 'NOT REFUSED'}] {utterance!r}\n    {reply[:200]!r}"
            )
            if not refused:
                problems.append(utterance)
    await asyncio.sleep(0.5)
    print(f"model_used per turn: {turn_models(caplog)}")
    assert problems == []


@live
async def test_live_luna_proposes_a_commitment(
    live_server: tuple[str, SpyTool], caplog: pytest.LogCaptureFixture
) -> None:
    url, spy = live_server
    caplog.set_level(logging.INFO, logger="relay.delegator.service")
    converged = [
        {"role": "user", "content": "I want a research doc on local LLMs for my Mac."},
        {"role": "assistant", "content": "Sure. What should it cover, and what's out of scope?"},
        {
            "role": "user",
            "content": "Compare the best open-weight models that fit in 64 GB on an M4 Pro: "
            "quality, speed and memory. Skip cloud models and fine-tuning. A markdown doc is "
            "all I need. That's everything, go ahead.",
        },
    ]
    hits = 0
    tries = 3
    async with httpx.AsyncClient(timeout=60) as client:
        for _ in range(tries):
            before = len(spy.calls)
            _, reply = await converse(client, url, body_for(converged, uuid.uuid4()))
            called = len(spy.calls) > before
            hits += called
            print(f"\n[{'PROPOSED' if called else 'no call'}] {reply[:200]!r}")
    await asyncio.sleep(0.5)
    print(f"propose_commitment rate: {hits}/{tries}; model_used: {turn_models(caplog)}")
    assert hits >= 2
