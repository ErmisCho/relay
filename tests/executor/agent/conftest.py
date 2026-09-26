"""Research fixtures: a worker running the REAL research runner (not ``fake_runners``).

``worker`` overrides the executor fixture of the same name, so ``dbos_client`` & co. talk to
this worker. By default it installs ``stub_models`` (no network, no LLM); the live test sets
``RELAY_LIVE_RESEARCH=1`` to run the configured Ollama models instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, insert

from relay.store.models import Commitment, Idea, Session, Turn
from tests.conftest import REPO_ROOT
from tests.executor.conftest import Seed, Worker

STUB_TIMEOUT_S = 3


class ResearchWorker(Worker):
    def __init__(
        self, db_url: str, flag_dir: Path, runner_modules: str, extra_env: dict[str, str]
    ) -> None:
        super().__init__(db_url, flag_dir)
        self.runner_modules = runner_modules
        self.extra_env = extra_env
        self.artifacts_dir = flag_dir / "artifacts"

    def start(self) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "EXECUTOR_PORT": str(self.port),
            "EXECUTOR_RUNNER_MODULES": self.runner_modules,
            "EXECUTOR_QUEUE_POLL_S": "0.1",
            "RELAY_FAKE_FLAG_DIR": str(self.flag_dir),
            "ARTIFACTS_DIR": str(self.artifacts_dir),
            "PYTHONPATH": str(REPO_ROOT),
            **self.extra_env,
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


def start_research_worker(
    exec_db_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    stub_timeout_s: float,
    stub_env: dict[str, str] | None = None,
) -> ResearchWorker:
    """Start a worker; unless live, with stub models/router and ``stub_env`` in its env."""
    live = os.environ.get("RELAY_LIVE_RESEARCH") == "1"
    modules = "" if live else "tests.executor.agent.stub_models"
    extra = {} if live else {"RELAY_RESEARCH_TIMEOUT_S": str(stub_timeout_s), **(stub_env or {})}
    w = ResearchWorker(exec_db_url, tmp_path_factory.mktemp("research"), modules, extra)
    w.start()
    return w


@pytest.fixture(scope="module")
def worker(exec_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResearchWorker]:
    # Stub runs finish in well under a second; a short bound lets a test hit the deadline.
    w = start_research_worker(exec_db_url, tmp_path_factory, STUB_TIMEOUT_S)
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
def seed_research(sync_engine: Engine) -> Callable[[str, str], Seed]:
    """Insert idea + session + assented research commitment with ``goal`` / ``excludes``."""

    def _seed(goal: str, excludes: str) -> Seed:
        idea_id, commitment_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with sync_engine.begin() as c:
            c.execute(insert(Idea).values(id=idea_id, title=goal, status="committed"))
            c.execute(insert(Session).values(id=session_id))
            # The spoken yes the commitment was agreed on, as the protocol records it (0003).
            turn_id = uuid.uuid4()
            c.execute(
                insert(Turn).values(id=turn_id, session_id=session_id, role="user", text="yes")
            )
            c.execute(
                insert(Commitment).values(
                    id=commitment_id,
                    session_id=session_id,
                    assent_turn_id=turn_id,
                    idea_id=idea_id,
                    goal=goal,
                    scope_excludes=excludes,
                    artifact_kind="document",
                    readback_text=f"I'll research {goal}.",
                    assent_utterance="yes",
                    assented_at=datetime.now(UTC),
                )
            )
        return Seed(idea_id, commitment_id, session_id)

    return _seed
