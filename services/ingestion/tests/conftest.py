import os

import pytest
import respx
from fakeredis import FakeAsyncRedis
from fsp_shared import db, migrate
from fsp_shared.bus import Bus
from fsp_shared.config import Settings
from fsp_shared.sim_client import SimClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.state import IngestionState, SafeBus
from app.sync import Syncer

from .helpers import BASE, FakeSim

URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if not URL:
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
        for item in items:
            if "engine" in item.fixturenames or "syncer" in item.fixturenames:
                item.add_marker(skip)


@pytest.fixture
async def engine():
    e = create_async_engine(URL)
    async with e.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await migrate.upgrade_head(URL)
    yield e
    await e.dispose()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL or "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("SIMULATOR_URL", BASE)
    return Settings(_env_file=None)


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def bus(redis):
    return Bus(redis)


@pytest.fixture
def sim():
    with respx.mock(assert_all_called=False) as router:
        yield FakeSim(router)


@pytest.fixture
def state():
    return IngestionState()


@pytest.fixture
async def syncer(engine, bus, state, settings, sim):
    async def no_sleep(_):
        return None

    client = SimClient(BASE, 3.0, sleep=no_sleep)
    factory = db.make_session_factory(engine)
    s = Syncer(client, factory, SafeBus(bus), state, settings)
    s.factory = factory
    yield s
    await client.aclose()


async def bus_types(redis, stream="fsp:events"):
    """All (type, payload) pairs published so far."""
    import json

    return [(f["type"], json.loads(f["payload"])) for _, f in await redis.xrange(stream)]
