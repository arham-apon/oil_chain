"""Phase 2 acceptance: ``alembic upgrade head`` builds the schema and it matches the ORM models.

Needs a disposable Postgres: set ``TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/dbname``
(the database is wiped). Skipped otherwise.
"""

import os
import uuid

import pytest
from sqlalchemy import DateTime, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.types import BigInteger, Boolean, Double, Integer, Text

from fsp_shared import db, migrate
from fsp_shared.models import Base, Decision, SimAllocation

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

EXPECTED_TABLES = {
    "sim_ticks",
    "depot_snapshots",
    "station_snapshots",
    "route_snapshots",
    "supply_arrivals",
    "sim_events",
    "demand_observations",
    "sim_allocations",
    "sim_metrics",
    "forecasts",
    "model_registry",
    "decisions",
    "alerts",
    "incidents",
    "audit_log",
    "ai_calls",
}


async def _reset(engine):
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture
async def engine():
    e = create_async_engine(URL)
    await _reset(e)
    await migrate.upgrade_head(URL)
    yield e
    await e.dispose()


def _kind(t):
    if isinstance(t, postgresql.JSONB):
        return "jsonb"
    if isinstance(t, postgresql.UUID):
        return "uuid"
    if isinstance(t, DateTime):
        return f"timestamp{'tz' if t.timezone else ''}"
    if isinstance(t, BigInteger):
        return "bigint"
    if isinstance(t, Integer):
        return "int"
    if isinstance(t, Double) or type(t).__name__ in ("DOUBLE_PRECISION", "Double"):
        return "double"
    if isinstance(t, Boolean):
        return "bool"
    if isinstance(t, Text):
        return "text"
    return type(t).__name__.lower()


async def test_upgrade_creates_all_tables_and_records_revision(engine):
    async with engine.connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert EXPECTED_TABLES <= tables and "alembic_version" in tables
    assert await db.schema_revision(engine) == "0002" == db.HEAD_REVISION
    await db.wait_for_schema(engine, timeout_s=1)


async def test_migration_matches_orm_models(engine):
    def compare(conn):
        insp = inspect(conn)
        problems = []
        for name, table in Base.metadata.tables.items():
            live = {c["name"]: c for c in insp.get_columns(name)}
            if set(live) != set(table.columns.keys()):
                problems.append(f"{name}: columns {sorted(live)} != {sorted(table.columns.keys())}")
                continue
            for col in table.columns:
                lc = live[col.name]
                if _kind(lc["type"]) != _kind(col.type):
                    problems.append(f"{name}.{col.name}: type {_kind(lc['type'])} != {_kind(col.type)}")
                if lc["nullable"] != (col.nullable and not col.primary_key):
                    problems.append(f"{name}.{col.name}: nullable {lc['nullable']} != {col.nullable}")
            pk = insp.get_pk_constraint(name)["constrained_columns"]
            if pk != [c.name for c in table.primary_key.columns]:
                problems.append(f"{name}: pk {pk}")
            live_idx = {i["name"] for i in insp.get_indexes(name)}
            orm_idx = {i.name for i in table.indexes}
            if orm_idx - live_idx:
                problems.append(f"{name}: missing indexes {orm_idx - live_idx}")
            live_uq = {tuple(u["column_names"]) for u in insp.get_unique_constraints(name)}
            for uc in table.constraints:
                if (
                    uc.__class__.__name__ == "UniqueConstraint"
                    and tuple(c.name for c in uc.columns) not in live_uq
                ):
                    problems.append(f"{name}: missing unique {[c.name for c in uc.columns]}")
        return problems

    async with engine.connect() as conn:
        assert await conn.run_sync(compare) == []


async def test_partial_index_and_descending_indexes(engine):
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'"))
        ).all()
    defs = {n: d for n, d in rows}
    assert "WHERE" in defs["ix_sim_allocations_active"] and "PENDING" in defs["ix_sim_allocations_active"]
    assert "tick DESC" in defs["ix_depot_snapshots_depot_tick"]
    assert "tick DESC" in defs["ix_station_snapshots_station_tick"]
    assert "tick DESC" in defs["ix_demand_obs_station_fuel_tick"]
    assert "cycle_tick DESC" in defs["ix_decisions_status_cycle_tick"]


async def test_upgrade_is_idempotent_and_downgrade_works(engine):
    await migrate.upgrade_head(URL)  # second run is a no-op
    assert await db.schema_revision(engine) == "0002"

    from alembic import command

    await __import__("asyncio").to_thread(command.downgrade, migrate.alembic_config(URL), "base")
    async with engine.connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert not (EXPECTED_TABLES & tables)


async def test_constraints_behave(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sim_ticks (tick, sim_time, status, tick_minutes) VALUES (1, now(), 'PAUSED', 15)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status) VALUES (1,'s','DIESEL',1,2,1,'OPEN')"
            )
        )
        # idempotency keys are free-form TEXT of up to 150 chars (C17), not UUID
        await conn.execute(
            text("INSERT INTO sim_allocations (id, idempotency_key) VALUES (1, :k)"), {"k": "x" * 150}
        )
        did = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO decisions (decision_id, idempotency_key, cycle_tick, quantity, system_origin, status, facts) VALUES (:i, :k, 1, 500, 'SYSTEM1_AUTO', 'PROPOSED', '{}'::jsonb)"
            ),
            {"i": did, "k": f"fsp-{did}"},
        )
    for stmt, params in [
        (
            "INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status) VALUES (1,'s','DIESEL',1,2,1,'OPEN')",
            {},
        ),
        ("INSERT INTO sim_allocations (id, idempotency_key) VALUES (2, :k)", {"k": "x" * 150}),
        (
            "INSERT INTO decisions (decision_id, idempotency_key, cycle_tick, quantity, system_origin, status, facts) VALUES (gen_random_uuid(), :k, 1, 1, 'X', 'Y', '{}'::jsonb)",
            {"k": f"fsp-{did}"},
        ),
    ]:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(stmt), params)
    async with engine.begin() as conn:  # upsert path used by ingestion
        await conn.execute(
            text(
                "INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status) VALUES (1,'s','DIESEL',5,2,1,'OPEN') ON CONFLICT (station_id, fuel_type, tick) DO UPDATE SET inventory = EXCLUDED.inventory"
            )
        )
        inv = (
            await conn.execute(text("SELECT inventory FROM station_snapshots WHERE station_id='s'"))
        ).scalar_one()
    assert inv == 5


async def test_orm_roundtrip(engine):
    from sqlalchemy import select

    factory = db.make_session_factory(engine)
    did = uuid.uuid4()
    async with db.session_scope(factory) as s:
        s.add(
            Decision(
                decision_id=did,
                idempotency_key=f"fsp-{did}",
                cycle_tick=3,
                quantity=1500.0,
                system_origin="HEURISTIC_FALLBACK",
                status="STAGED_REVIEW",
                facts={"a": 1},
                jev={"urgency_1to5": 4.2},
            )
        )
        s.add(SimAllocation(id=7, idempotency_key="k-7", quantity=10.0, status="PENDING"))
    async with db.session_scope(factory) as s:
        d = (await s.execute(select(Decision))).scalar_one()
        assert d.decision_id == did and d.jev == {"urgency_1to5": 4.2} and d.created_at is not None
        a = (await s.execute(select(SimAllocation))).scalar_one()
        assert a.status == "PENDING" and a.updated_at is not None
    with pytest.raises(RuntimeError):
        async with db.session_scope(factory) as s:  # rolled back on error
            s.add(SimAllocation(id=8, idempotency_key="k-8"))
            raise RuntimeError("boom")
    async with db.session_scope(factory) as s:
        assert len((await s.execute(select(SimAllocation))).scalars().all()) == 1


async def test_wait_for_schema_times_out_on_empty_db():
    e = create_async_engine(URL)
    await _reset(e)
    with pytest.raises(TimeoutError):
        await db.wait_for_schema(e, timeout_s=0.5, interval_s=0.1)
    assert await db.ping(e) is True
    await e.dispose()
