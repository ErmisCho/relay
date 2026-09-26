"""DBOS wiring shared by the executor worker and the Delegator-side client.

This module is safe to import from the Delegator: it never configures or launches DBOS,
it only knows the queue/workflow names and how to build a ``DBOSClient``.

System database: DBOS keeps its bookkeeping tables in the ``dbos`` schema of the SAME
Postgres database as the idea graph (``Settings.database_url``), so no extra
``<db>_dbos_sys`` database is ever created. Override with ``DBOS_SYSTEM_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time

from dbos import DBOSClient, run_dbos_database_migrations

from relay.config import Settings, to_libpq_url

log = logging.getLogger(__name__)

APP_NAME = "relay"
QUEUE_NAME = "relay-tasks"
WORKFLOW_NAME = "relay.run_task"
SYSTEM_SCHEMA = "dbos"

# DBOS only recovers PENDING workflows whose application_version matches the running one, and
# by default that version is a hash of the workflow source. Pin it, so editing workflow code
# between a crash and a restart does not orphan in-flight tasks. Bump (or set
# RELAY_APP_VERSION) only for changes that make old in-flight workflows unreplayable.
DEFAULT_APP_VERSION = "relay-1"


def app_version() -> str:
    return os.environ.get("RELAY_APP_VERSION", DEFAULT_APP_VERSION)


def workflow_id_for(task_id: object) -> str:
    """Deterministic DBOS workflow id for a task row."""
    return f"task-{task_id}"


def one_sentence(text: str | None, fallback: str) -> str:
    """First sentence of ``text`` (<= 200 chars), or ``fallback``."""
    if not text or not text.strip():
        return fallback
    first = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    return first if len(first) <= 200 else first[:197].rstrip() + "..."


def system_database_url(settings: Settings) -> str:
    """Driver-less Postgres URL of the DBOS system database (default: the app database)."""
    override = os.environ.get("DBOS_SYSTEM_DATABASE_URL")
    return to_libpq_url(override) if override else settings.libpq_database_url


_clients: dict[str, DBOSClient] = {}
_failures: dict[str, tuple[float, BaseException]] = {}
_lock = threading.Lock()
FAILURE_TTL_S = 30.0


class DBOSUnavailableError(RuntimeError):
    """The DBOS system database could not be reached (possibly a cached recent failure)."""


def get_dbos_client(settings: Settings) -> DBOSClient:
    """Return a process-wide ``DBOSClient`` for ``settings`` (created once per URL).

    Blocking (migrations + connect, ~100+ ms cold): from async code use
    ``get_dbos_client_async``. Runs the (idempotent, advisory-locked) DBOS schema migrations
    first, so the Delegator can enqueue work even before the executor has ever launched; the
    task then starts as soon as a worker comes up. A failure is remembered for
    ``FAILURE_TTL_S`` so an unreachable database does not cost a connect attempt per call.
    """
    url = system_database_url(settings)
    with _lock:
        client = _clients.get(url)
        if client is not None:
            return client
        failed = _failures.get(url)
        if failed is not None and time.monotonic() - failed[0] < FAILURE_TTL_S:
            raise DBOSUnavailableError("DBOS system database recently unreachable") from failed[1]
        try:
            run_dbos_database_migrations(url, schema=SYSTEM_SCHEMA)
            client = DBOSClient(
                system_database_url=url,
                dbos_system_schema=SYSTEM_SCHEMA,
                application_name=APP_NAME,
            )
        except Exception as exc:
            _failures[url] = (time.monotonic(), exc)
            raise DBOSUnavailableError("DBOS system database unreachable") from exc
        _failures.pop(url, None)
        _clients[url] = client
        return client


async def get_dbos_client_async(settings: Settings) -> DBOSClient:
    """``get_dbos_client`` off the event loop."""
    return await asyncio.to_thread(get_dbos_client, settings)


async def warm_dbos_client(settings: Settings) -> bool:
    """Create the client at Delegator startup so the first dispatch stays fast.

    Returns False (and logs) instead of raising when DBOS is unreachable.
    """
    try:
        await get_dbos_client_async(settings)
    except DBOSUnavailableError:
        log.warning("DBOS client warm-up failed; will retry lazily", exc_info=True)
        return False
    return True


def close_dbos_clients() -> None:
    """Dispose every cached client and forget cached failures (tests, process shutdown)."""
    with _lock:
        for client in _clients.values():
            client.destroy()
        _clients.clear()
        _failures.clear()
