"""ORM tables (spec §8). Kept in lock-step with Alembic migration ``0001`` — ``tests/test_schema.py`` fails on drift.

Naming follows the simulator (``fuel_type``). All timestamps are ``TIMESTAMPTZ``. Idempotency keys are
``TEXT`` (C17): the simulator accepts any string of length 1–150.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NOW = func.now()


class Base(DeclarativeBase):
    pass


def _tz() -> DateTime:
    return DateTime(timezone=True)


class SimTick(Base):
    __tablename__ = "sim_ticks"
    tick: Mapped[int] = mapped_column(Integer, primary_key=True)
    sim_time: Mapped[datetime] = mapped_column(_tz(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tick_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    recorded_at: Mapped[datetime] = mapped_column(_tz(), nullable=False, server_default=NOW)


class DepotSnapshot(Base):
    __tablename__ = "depot_snapshots"
    __table_args__ = (
        UniqueConstraint("depot_id", "fuel_type", "tick", name="uq_depot_snapshots_depot_fuel_tick"),
        Index("ix_depot_snapshots_depot_tick", "depot_id", text("tick DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    depot_id: Mapped[str] = mapped_column(Text, nullable=False)
    fuel_type: Mapped[str] = mapped_column(Text, nullable=False)
    inventory: Mapped[float] = mapped_column(Double, nullable=False)
    capacity: Mapped[float] = mapped_column(Double, nullable=False)
    dispatch_capacity_per_tick: Mapped[float] = mapped_column(Double, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class StationSnapshot(Base):
    __tablename__ = "station_snapshots"
    __table_args__ = (
        UniqueConstraint("station_id", "fuel_type", "tick", name="uq_station_snapshots_station_fuel_tick"),
        Index("ix_station_snapshots_station_tick", "station_id", text("tick DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    station_id: Mapped[str] = mapped_column(Text, nullable=False)
    fuel_type: Mapped[str] = mapped_column(Text, nullable=False)
    inventory: Mapped[float] = mapped_column(Double, nullable=False)
    capacity: Mapped[float] = mapped_column(Double, nullable=False)
    demand_multiplier: Mapped[float] = mapped_column(Double, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class RouteSnapshot(Base):
    __tablename__ = "route_snapshots"
    __table_args__ = (UniqueConstraint("route_id", "tick", name="uq_route_snapshots_route_tick"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    route_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    transit_ticks: Mapped[int] = mapped_column(Integer, nullable=False)
    max_shipment: Mapped[float] = mapped_column(Double, nullable=False)


class SupplyArrivalRow(Base):
    __tablename__ = "supply_arrivals"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    depot_id: Mapped[str | None] = mapped_column(Text)
    fuel_type: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float | None] = mapped_column(Double)
    planned_tick: Mapped[int | None] = mapped_column(Integer)
    actual_tick: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class SimEventRow(Base):
    __tablename__ = "sim_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str | None] = mapped_column(Text)
    start_tick: Mapped[int | None] = mapped_column(Integer)
    end_tick: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    first_seen_tick: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class DemandObservation(Base):
    __tablename__ = "demand_observations"
    __table_args__ = (
        UniqueConstraint("station_id", "fuel_type", "tick", name="uq_demand_obs_station_fuel_tick"),
        Index("ix_demand_obs_station_fuel_tick", "station_id", "fuel_type", text("tick DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # simulator row id
    station_id: Mapped[str] = mapped_column(Text, nullable=False)
    fuel_type: Mapped[str] = mapped_column(Text, nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    sim_time: Mapped[datetime] = mapped_column(_tz(), nullable=False)
    demand_liters: Mapped[float | None] = mapped_column(Double)
    served_liters: Mapped[float | None] = mapped_column(Double)
    unmet_liters: Mapped[float | None] = mapped_column(Double)


class SimAllocation(Base):
    __tablename__ = "sim_allocations"
    __table_args__ = (
        Index(
            "ix_sim_allocations_active",
            "status",
            postgresql_where=text("status IN ('PENDING','IN_TRANSIT')"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # simulator id
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    source_depot_id: Mapped[str | None] = mapped_column(Text)
    destination_station_id: Mapped[str | None] = mapped_column(Text)
    route_id: Mapped[str | None] = mapped_column(Text)
    fuel_type: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float | None] = mapped_column(Double)
    created_tick: Mapped[int | None] = mapped_column(Integer)
    departure_tick: Mapped[int | None] = mapped_column(Integer)
    expected_arrival_tick: Mapped[int | None] = mapped_column(Integer)
    actual_arrival_tick: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class SimMetric(Base):
    __tablename__ = "sim_metrics"
    tick: Mapped[int] = mapped_column(Integer, primary_key=True)
    served: Mapped[float | None] = mapped_column(Double)
    unmet: Mapped[float | None] = mapped_column(Double)
    service_level: Mapped[float | None] = mapped_column(Double)
    allocation_liters: Mapped[float | None] = mapped_column(Double)
    allocation_failures: Mapped[int | None] = mapped_column(Integer)


class Forecast(Base):
    __tablename__ = "forecasts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    made_at_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    station_id: Mapped[str | None] = mapped_column(Text)
    fuel_type: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    horizon: Mapped[Any] = mapped_column(JSONB, nullable=False)  # [{tick, mean, sigma}]
    residual_sigma: Mapped[float | None] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # MODEL | BASELINE_FALLBACK


class ModelRegistry(Base):
    __tablename__ = "model_registry"
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    trained_at_tick: Mapped[int | None] = mapped_column(Integer)
    n_rows: Mapped[int | None] = mapped_column(Integer)
    mae: Mapped[float | None] = mapped_column(Double)
    rmse: Mapped[float | None] = mapped_column(Double)
    baseline_mae: Mapped[float | None] = mapped_column(Double)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_status_cycle_tick", "status", text("cycle_tick DESC")),)
    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # 'fsp-' || decision_id
    cycle_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    station_id: Mapped[str | None] = mapped_column(Text)
    fuel_type: Mapped[str | None] = mapped_column(Text)
    source_depot_id: Mapped[str | None] = mapped_column(Text)
    route_id: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Double, nullable=False)
    # SYSTEM1_AUTO | SYSTEM2_OVERRIDE | HEURISTIC_FALLBACK | OPERATOR_MANUAL
    system_origin: Mapped[str] = mapped_column(Text, nullable=False)
    # PROPOSED|STAGED_REVIEW|AUTO_APPROVED|OPERATOR_APPROVED|REJECTED|EXPIRED|COMMITTING|COMMITTED|
    # SIM_REJECTED|CANCELLED|SUPERSEDED
    status: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[Any] = mapped_column(JSONB, nullable=False)
    jev: Mapped[Any | None] = mapped_column(JSONB)
    explanation: Mapped[Any | None] = mapped_column(JSONB)
    sim_allocation_id: Mapped[int | None] = mapped_column(Integer)
    sim_error_code: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    operator_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)
    updated_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tick: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    fuel_type: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    data: Mapped[Any | None] = mapped_column(JSONB)
    acknowledged: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sim_event_id: Mapped[int | None] = mapped_column(Integer)
    brief: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)
    tick: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    data: Mapped[Any | None] = mapped_column(JSONB)


class AICall(Base):
    __tablename__ = "ai_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime | None] = mapped_column(_tz(), server_default=NOW)
    provider: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
