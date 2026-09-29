"""Async SQLAlchemy engine/session helpers and the schema-readiness wait used by non-owner services.

ingestion-svc owns migrations (:func:`fsp_shared.migrate.upgrade_head`); every other service calls
:func:`wait_for_schema` on startup instead of touching DDL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

HEAD_REVISION = "0002"


def make_engine(database_url: str, *, pool_size: int = 10, echo: bool = False) -> AsyncEngine:
    """Engine with ``pool_pre_ping`` so a Postgres restart is survived (connections are re-validated)."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=10,
        pool_recycle=1800,
        echo=echo,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def schema_revision(engine: AsyncEngine) -> str | None:
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
            return row[0] if row else None
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        return None


async def wait_for_schema(
    engine: AsyncEngine,
    revision: str = HEAD_REVISION,
    *,
    timeout_s: float = 120.0,
    interval_s: float = 1.0,
) -> None:
    """Block until ingestion-svc has migrated the database to ``revision`` (or raise ``TimeoutError``)."""
    deadline = time.monotonic() + timeout_s
    while True:
        if await schema_revision(engine) == revision:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"database schema did not reach revision {revision} within {timeout_s}s")
        await asyncio.sleep(interval_s)


async def ping(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        return False
