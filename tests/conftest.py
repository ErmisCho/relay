"""Shared fixtures: a throwaway, fully-migrated Postgres database per test session.

The server comes from `TEST_DATABASE_URL` if set, else `Settings.database_url` (the compose
Postgres on :55432). Each pytest session creates a uniquely-named database, runs
`alembic upgrade head` against it, and drops it afterwards. If the server is unreachable every
test that needs the database is skipped (never failed). Later waves reuse `migrated_db_url`,
`db_engine` / `db_conn` (sync) and `make_database` (extra fresh DBs).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import make_url

from relay.config import get_settings, to_libpq_url, to_sync_url

REPO_ROOT = Path(__file__).resolve().parents[1]


def _server_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


def alembic_config(database_url: str) -> Config:
    """Alembic config pointed at `database_url` (any SQLAlchemy Postgres URL form)."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def make_database() -> Iterator[Callable[[], str]]:
    """Factory creating empty uniquely-named databases; returns their SQLAlchemy URL.

    All databases created through the factory are dropped at the end of the session.
    """
    admin_url = to_libpq_url(_server_url())
    try:
        psycopg.connect(admin_url, connect_timeout=3, autocommit=True).close()
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"Postgres unreachable at {make_url(admin_url).render_as_string()} "
            f"(run `docker compose up -d postgres`): {exc}"
        )

    created: list[str] = []

    def _make() -> str:
        name = f"relay_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return make_url(_server_url()).set(database=name).render_as_string(hide_password=False)

    yield _make

    with psycopg.connect(admin_url, autocommit=True) as conn:
        for name in created:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def migrated_db_url(make_database: Callable[[], str]) -> str:
    """URL of a fresh database migrated to `head` (shared for the whole test session)."""
    url = make_database()
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
def db_engine(migrated_db_url: str) -> Iterator[Engine]:
    engine = create_engine(to_sync_url(migrated_db_url))
    yield engine
    engine.dispose()


@pytest.fixture
def db_conn(db_engine: Engine) -> Iterator[Connection]:
    """Connection inside a transaction that is rolled back after the test."""
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
