"""Async engine and session factory for the idea graph."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from relay.config import get_settings, to_async_url


def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Create an asyncpg engine; defaults to `Settings.database_url`."""
    return create_async_engine(to_async_url(url or get_settings().database_url), echo=echo)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
