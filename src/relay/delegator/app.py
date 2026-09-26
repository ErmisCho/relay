"""FastAPI app: the OpenAI-compatible custom-LLM endpoint ElevenLabs calls."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from relay.config import Settings, get_settings
from relay.delegator import wiring
from relay.delegator.auth import bearer_auth, check_secret_is_safe
from relay.delegator.commitment.reconcile import start_orphaned_commitments
from relay.delegator.contracts import SessionStore, ToolRegistry, TurnHook
from relay.delegator.demo import api as demo_api
from relay.delegator.llm import ChatModel, build_chat_model
from relay.delegator.service import ChatCompletionRequest, DelegatorService
from relay.executor.common import warm_dbos_client
from relay.store.db import create_engine, create_sessionmaker

log = logging.getLogger(__name__)

RECONCILE_TIMEOUT_S = 15.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
CHAT_COMPLETIONS_PATHS = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/v1/chat/completions",
    "/v1",
)


def create_app(
    settings: Settings | None = None,
    *,
    chat_model: ChatModel | None = None,
    registry: ToolRegistry | None = None,
    hooks: list[TurnHook] | None = None,
    session_store: SessionStore | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    warm_dbos: bool = True,
) -> FastAPI:
    """Build the Delegator app; every collaborator is injectable for tests.

    ``warm_dbos`` creates the DBOS client at startup so the first dispatch meets its
    latency budget, then starts any commitment left without a task (see
    ``commitment.reconcile``); tests pass False.
    """
    settings = settings or get_settings()
    check_secret_is_safe(settings)

    engine: AsyncEngine | None = None
    if sessionmaker is None:
        engine = create_engine(settings.database_url)
        sessionmaker = create_sessionmaker(engine)

    service = DelegatorService(
        settings=settings,
        chat_model=chat_model or build_chat_model(settings),
        registry=registry if registry is not None else wiring.build_registry(),
        hooks=hooks if hooks is not None else wiring.build_hooks(),
        session_store=session_store or SessionStore(),
        sessionmaker=sessionmaker,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if warm_dbos:
            try:
                warmed = await warm_dbos_client(settings)
            except Exception:  # documented never to raise; startup must not depend on it
                log.warning("DBOS client warm-up raised; continuing", exc_info=True)
                warmed = False
            log.info("DBOS client warm-up %s", "succeeded" if warmed else "failed")
            # Commitments whose dispatch was cut short (crash between commit and start_task).
            try:
                async with asyncio.timeout(RECONCILE_TIMEOUT_S):
                    await start_orphaned_commitments(sessionmaker, settings)
            except Exception:
                log.warning("startup reconciliation of commitments failed", exc_info=True)
        if demo is not None:
            demo.start()
        yield
        if demo is not None:
            await demo.stop()
        await service.drain()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="relay delegator", lifespan=lifespan)
    app.state.service = service

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def chat_completions(body: ChatCompletionRequest, request: Request) -> Response:
        received_at = time.perf_counter()
        log.info("delegator: chat request on %s", request.url.path)
        turn = await service.prepare(body, request.headers, received_at)
        if not body.stream:
            return JSONResponse(await service.complete(turn))
        return StreamingResponse(
            service.stream_sse(turn), media_type="text/event-stream", headers=SSE_HEADERS
        )

    # ElevenLabs' docs don't say whether the custom-LLM "Server URL" is a base URL it
    # appends /chat/completions (or /v1/chat/completions) to, or the full endpoint.
    # Serve every interpretation; the request log above shows which one it uses.
    for path in CHAT_COMPLETIONS_PATHS:
        app.add_api_route(
            path,
            chat_completions,
            methods=["POST"],
            dependencies=[Depends(bearer_auth(settings.delegator_shared_secret))],
            response_model=None,
        )

    # Demo website API (TASK-42): absent (404) unless DEMO_PASSCODE is set.
    demo = demo_api.install(app, settings, service, sessionmaker)
    web_dist = Path(settings.demo_web_dist)
    if demo is not None and settings.demo_web_dist and web_dist.is_dir():
        # Mounted last so every API route above wins; same origin as /demo over ngrok.
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app
