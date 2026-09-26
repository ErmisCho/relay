"""Executor fixtures: a migrated database plus a real worker subprocess on it.

DBOS system tables live in the ``dbos`` schema of that same database, so dropping it (done by
the root ``make_database`` fixture) removes everything DBOS created.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, create_engine, insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings, to_sync_url
from relay.executor.common import close_dbos_clients, get_dbos_client
from relay.store.db import create_engine as create_async_engine
from relay.store.db import create_sessionmaker
from relay.store.models import Commitment, Idea, Session
from tests.conftest import REPO_ROOT, alembic_config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Worker:
    """The executor as a real process, so tests can SIGKILL and restart it."""

    def __init__(self, db_url: str, flag_dir: Path) -> None:
        self.db_url, self.flag_dir = db_url, flag_dir
        self.port = _free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self.log = flag_dir / "worker.log"

    def start(self) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "EXECUTOR_PORT": str(self.port),
            "EXECUTOR_RUNNER_MODULES": "tests.executor.fake_runners",
            "EXECUTOR_QUEUE_POLL_S": "0.1",
            "RELAY_FAKE_FLAG_DIR": str(self.flag_dir),
            "PYTHONPATH": str(REPO_ROOT),
        }
        env.pop("DBOS_SYSTEM_DATABASE_URL", None)
        with open(self.log, "ab") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "relay.executor"],
                cwd=REPO_ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"worker exited early:\n{self.log.read_text()[-3000:]}")
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=1).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"worker never became healthy:\n{self.log.read_text()[-3000:]}")

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)
        self.proc = None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill()
        self.proc = None


@pytest.fixture(scope="module")
def exec_db_url(make_database: Callable[[], str]) -> str:
    from alembic import command

    url = make_database()
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture(scope="module")
def sync_engine(exec_db_url: str) -> Iterator[Engine]:
    engine = create_engine(to_sync_url(exec_db_url))
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def worker(exec_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worker]:
    w = Worker(exec_db_url, tmp_path_factory.mktemp("executor"))
    w.start()
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture(scope="module")
def dbos_client(exec_db_url: str, worker: Worker) -> Iterator[DBOSClient]:
    yield get_dbos_client(Settings(database_url=exec_db_url))
    close_dbos_clients()


@pytest.fixture
async def db(exec_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(exec_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@dataclass(frozen=True)
class Seed:
    idea_id: uuid.UUID
    commitment_id: uuid.UUID
    session_id: uuid.UUID


@pytest.fixture
def seed(sync_engine: Engine) -> Callable[[str], Seed]:
    """Insert idea + session + assented commitment with ``goal``."""

    def _seed(goal: str) -> Seed:
        idea_id, commitment_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with sync_engine.begin() as c:
            c.execute(insert(Idea).values(id=idea_id, title=goal, status="committed"))
            c.execute(insert(Session).values(id=session_id))
            c.execute(
                insert(Commitment).values(
                    id=commitment_id,
                    idea_id=idea_id,
                    goal=goal,
                    scope_excludes="no bookings",
                    artifact_kind="document",
                    readback_text=f"I'll research {goal}.",
                    assent_utterance="yes",
                    assented_at=datetime.now(UTC),
                )
            )
        return Seed(idea_id, commitment_id, session_id)

    return _seed


@pytest.fixture
def no_pending_reports(sync_engine: Engine) -> None:
    """Hook tests see every undelivered report in the DB; start each from a clean slate."""
    with sync_engine.begin() as c:
        c.execute(
            text("UPDATE pending_reports SET delivered_at = now() WHERE delivered_at IS NULL")
        )


def wait_for(predicate: Callable[[], object], timeout: float = 30, what: str = "") -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what or predicate}")


def task_row(engine: Engine, task_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as c:
        return dict(
            c.execute(text("SELECT * FROM tasks WHERE id = :id"), {"id": task_id}).mappings().one()
        )
