"""Executor worker: a dedicated process that launches DBOS and runs queued tasks.

Kept separate from the Delegator so long-running work never competes with voice latency.
On launch DBOS recovers this executor's PENDING workflows (e.g. after a SIGKILL) and then
dequeues from ``QUEUE_NAME``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dbos import DBOS, DBOSConfig
from fastapi import FastAPI

from relay.config import Settings, get_settings
from relay.executor import workflows
from relay.executor.common import (
    APP_NAME,
    QUEUE_NAME,
    SYSTEM_SCHEMA,
    app_version,
    close_dbos_clients,
    get_dbos_client_async,
    system_database_url,
)
from relay.executor.dispatch import reconcile_orphaned_tasks
from relay.executor.runners import load_runner_modules, registered_kinds


def dbos_config(settings: Settings) -> DBOSConfig:
    return {
        "name": APP_NAME,
        "system_database_url": system_database_url(settings),
        "dbos_system_schema": SYSTEM_SCHEMA,
        "run_migrations": True,
        "application_version": app_version(),  # pinned; see common.DEFAULT_APP_VERSION
    }


def queue_polling_interval() -> float:
    return float(os.environ.get("EXECUTOR_QUEUE_POLL_S", "1.0"))


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        load_runner_modules()
        workflows.configure_engine(cfg.database_url)
        DBOS(config=dbos_config(cfg))
        DBOS.launch()
        await DBOS.register_queue_async(QUEUE_NAME, polling_interval_sec=queue_polling_interval())
        client = await get_dbos_client_async(cfg)
        await asyncio.to_thread(reconcile_orphaned_tasks, workflows.engine(), client)
        try:
            yield
        finally:
            close_dbos_clients()
            DBOS.destroy()

    app = FastAPI(title="relay executor", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "kinds": registered_kinds()}

    return app
