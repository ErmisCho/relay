"""Code-task fixtures: a worker running the REAL code runner with a stub agent and fake GitHub."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from relay.store.models import Commitment, Idea, Session, Turn
from tests.executor.agent.conftest import ResearchWorker
from tests.executor.conftest import Seed

MODULES = "tests.executor.agent.stub_models,tests.executor.code.stub_code"
FAKE_API = "https://github.test/api"
GOAL_EXCLUDES = "no changes to the CI configuration"


def code_worker(db_url: str, tmp: pytest.TempPathFactory, token: str) -> ResearchWorker:
    env = {
        "RELAY_RESEARCH_TIMEOUT_S": "60",
        "ENABLED_KINDS": "research,code",
        "GITHUB_TOKEN": token,
        "GITHUB_OWNER": "",
        "GITHUB_API_URL": FAKE_API,
        "RELAY_FAKE_GITHUB": "1",
    }
    w = ResearchWorker(db_url, tmp.mktemp("code"), MODULES, env)
    w.start()
    return w


@pytest.fixture(scope="module")
def worker(exec_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResearchWorker]:
    w = code_worker(exec_db_url, tmp_path_factory, token="fake-token")
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
def seed_code(sync_engine: Engine) -> Callable[[str], Seed]:
    """Idea + session + assent turn + assented CODE commitment with ``goal``."""

    def _seed(goal: str) -> Seed:
        idea_id, commitment_id, session_id, turn_id = (uuid.uuid4() for _ in range(4))
        with sync_engine.begin() as c:
            c.execute(insert(Idea).values(id=idea_id, title=goal, status="committed"))
            c.execute(insert(Session).values(id=session_id))
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
                    scope_excludes=GOAL_EXCLUDES,
                    artifact_kind="pull_request",
                    readback_text=f"I'll {goal}.",
                    assent_utterance="yes",
                    assented_at=datetime.now(UTC),
                )
            )
        return Seed(idea_id, commitment_id, session_id)

    return _seed


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def github_project(folder: Path, remote_root: Path) -> Path:
    """An existing project with origin github.com/acme/widget, rewritten to a local bare repo."""
    remote_root.mkdir(parents=True, exist_ok=True)
    bare = remote_root / "widget.git"
    sh(remote_root, "git", "init", "--quiet", "--bare", "--initial-branch", "main", str(bare))
    folder.mkdir(parents=True)
    sh(folder, "git", "init", "--quiet", "--initial-branch", "main")
    (folder / "pyproject.toml").write_text('[project]\nname = "widget"\nversion = "0"\n')
    ident = ("-c", "user.name=t", "-c", "user.email=t@t")
    sh(folder, "git", "add", "pyproject.toml")
    sh(folder, "git", *ident, "commit", "--quiet", "-m", "init")
    sh(folder, "git", "remote", "add", "origin", "https://github.com/acme/widget.git")
    sh(folder, "git", "config", f"url.{bare}.insteadOf", "https://github.com/acme/widget.git")
    sh(folder, "git", "push", "--quiet", "origin", "main")
    return bare
