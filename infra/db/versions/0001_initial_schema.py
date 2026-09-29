"""initial schema (spec §8)

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = [
    "ai_calls",
    "audit_log",
    "incidents",
    "alerts",
    "decisions",
    "model_registry",
    "forecasts",
    "sim_metrics",
    "sim_allocations",
    "demand_observations",
    "sim_events",
    "supply_arrivals",
    "route_snapshots",
    "station_snapshots",
    "depot_snapshots",
    "sim_ticks",
]

DDL = [
    """
    CREATE TABLE sim_ticks (
      tick INT PRIMARY KEY, sim_time TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
      tick_minutes INT NOT NULL, stale BOOLEAN NOT NULL DEFAULT false,
      recorded_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    """
    CREATE TABLE depot_snapshots (
      id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, depot_id TEXT NOT NULL, fuel_type TEXT NOT NULL,
      inventory DOUBLE PRECISION NOT NULL, capacity DOUBLE PRECISION NOT NULL,
      dispatch_capacity_per_tick DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
      stale BOOLEAN NOT NULL DEFAULT false,
      CONSTRAINT uq_depot_snapshots_depot_fuel_tick UNIQUE (depot_id, fuel_type, tick))
    """,
    "CREATE INDEX ix_depot_snapshots_depot_tick ON depot_snapshots (depot_id, tick DESC)",
    """
    CREATE TABLE station_snapshots (
      id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, station_id TEXT NOT NULL, fuel_type TEXT NOT NULL,
      inventory DOUBLE PRECISION NOT NULL, capacity DOUBLE PRECISION NOT NULL,
      demand_multiplier DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, stale BOOLEAN NOT NULL DEFAULT false,
      CONSTRAINT uq_station_snapshots_station_fuel_tick UNIQUE (station_id, fuel_type, tick))
    """,
    "CREATE INDEX ix_station_snapshots_station_tick ON station_snapshots (station_id, tick DESC)",
    """
    CREATE TABLE route_snapshots (
      id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, route_id TEXT NOT NULL, status TEXT NOT NULL,
      transit_ticks INT NOT NULL, max_shipment DOUBLE PRECISION NOT NULL,
      CONSTRAINT uq_route_snapshots_route_tick UNIQUE (route_id, tick))
    """,
    """
    CREATE TABLE supply_arrivals (
      id TEXT PRIMARY KEY, depot_id TEXT, fuel_type TEXT, quantity DOUBLE PRECISION,
      planned_tick INT, actual_tick INT, status TEXT, updated_at TIMESTAMPTZ DEFAULT now())
    """,
    """
    CREATE TABLE sim_events (
      id INT PRIMARY KEY, type TEXT, start_tick INT, end_tick INT, status TEXT,
      parameters JSONB, first_seen_tick INT, updated_at TIMESTAMPTZ DEFAULT now())
    """,
    """
    CREATE TABLE demand_observations (
      id BIGINT PRIMARY KEY,
      station_id TEXT NOT NULL, fuel_type TEXT NOT NULL, tick INT NOT NULL, sim_time TIMESTAMPTZ NOT NULL,
      demand_liters DOUBLE PRECISION, served_liters DOUBLE PRECISION, unmet_liters DOUBLE PRECISION,
      CONSTRAINT uq_demand_obs_station_fuel_tick UNIQUE (station_id, fuel_type, tick))
    """,
    "CREATE INDEX ix_demand_obs_station_fuel_tick ON demand_observations (station_id, fuel_type, tick DESC)",
    """
    CREATE TABLE sim_allocations (
      id INT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, source_depot_id TEXT,
      destination_station_id TEXT, route_id TEXT, fuel_type TEXT, quantity DOUBLE PRECISION,
      created_tick INT, departure_tick INT, expected_arrival_tick INT, actual_arrival_tick INT,
      status TEXT, failure_reason TEXT, updated_at TIMESTAMPTZ DEFAULT now())
    """,
    "CREATE INDEX ix_sim_allocations_active ON sim_allocations (status) WHERE status IN ('PENDING','IN_TRANSIT')",
    """
    CREATE TABLE sim_metrics (
      tick INT PRIMARY KEY, served DOUBLE PRECISION, unmet DOUBLE PRECISION,
      service_level DOUBLE PRECISION, allocation_liters DOUBLE PRECISION, allocation_failures INT)
    """,
    """
    CREATE TABLE forecasts (
      id BIGSERIAL PRIMARY KEY, made_at_tick INT NOT NULL, station_id TEXT, fuel_type TEXT,
      model_version TEXT, horizon JSONB NOT NULL,
      residual_sigma DOUBLE PRECISION, source TEXT NOT NULL)
    """,
    """
    CREATE TABLE model_registry (
      version TEXT PRIMARY KEY, trained_at_tick INT, n_rows INT, mae DOUBLE PRECISION, rmse DOUBLE PRECISION,
      baseline_mae DOUBLE PRECISION, artifact_path TEXT, active BOOLEAN DEFAULT false,
      created_at TIMESTAMPTZ DEFAULT now())
    """,
    """
    CREATE TABLE decisions (
      decision_id UUID PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
      cycle_tick INT NOT NULL, station_id TEXT, fuel_type TEXT, source_depot_id TEXT, route_id TEXT,
      quantity DOUBLE PRECISION NOT NULL,
      system_origin TEXT NOT NULL,
      status TEXT NOT NULL,
      facts JSONB NOT NULL,
      jev JSONB,
      explanation JSONB,
      sim_allocation_id INT, sim_error_code TEXT,
      operator TEXT, operator_note TEXT,
      created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())
    """,
    "CREATE INDEX ix_decisions_status_cycle_tick ON decisions (status, cycle_tick DESC)",
    """
    CREATE TABLE alerts (
      id BIGSERIAL PRIMARY KEY, tick INT, kind TEXT, severity TEXT, entity_id TEXT,
      fuel_type TEXT, message TEXT, data JSONB, acknowledged BOOLEAN DEFAULT false,
      created_at TIMESTAMPTZ DEFAULT now())
    """,
    """
    CREATE TABLE incidents (
      id BIGSERIAL PRIMARY KEY, sim_event_id INT, brief JSONB, source TEXT,
      created_at TIMESTAMPTZ DEFAULT now())
    """,
    """
    CREATE TABLE audit_log (
      id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ DEFAULT now(), tick INT, actor TEXT,
      action TEXT, entity_type TEXT, entity_id TEXT, result TEXT, data JSONB)
    """,
    """
    CREATE TABLE ai_calls (
      id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ DEFAULT now(), provider TEXT, purpose TEXT,
      latency_ms INT, ok BOOLEAN, error TEXT, input_tokens INT, output_tokens INT)
    """,
]


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")













