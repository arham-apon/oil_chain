"""Programmatic ``alembic upgrade head`` — called by ingestion-svc at startup (it owns migrations)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def default_script_location() -> str:
    """``FSP_ALEMBIC_DIR`` if set (Docker images), else ``<repo>/infra/db``."""
    env = os.environ.get("FSP_ALEMBIC_DIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "infra" / "db")


def alembic_config(database_url: str, script_location: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", script_location or default_script_location())
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade_head_sync(database_url: str, script_location: str | None = None) -> None:
    command.upgrade(alembic_config(database_url, script_location), "head")


async def upgrade_head(database_url: str, script_location: str | None = None) -> None:
    """Run migrations in a worker thread (alembic's env.py starts its own event loop)."""
    await asyncio.to_thread(upgrade_head_sync, database_url, script_location)
