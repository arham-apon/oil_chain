"""Pydantic DTOs shared between services: simulator payloads, client envelopes, bus events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .timeutil import parse_sim_time

FuelType = Literal["DIESEL", "PETROL", "OCTANE"]
SimStatus = Literal["PAUSED", "RUNNING"]
DepotStatus = Literal["OPEN", "CONSTRAINED"]
StationStatus = Literal["OPEN", "OUTAGE"]
RouteStatus = Literal["AVAILABLE", "DISRUPTED"]
SupplyStatus = Literal["SCHEDULED", "DELAYED", "ARRIVED"]
EventStatus = Literal["SCHEDULED", "ACTIVE", "RESOLVED"]
AllocationStatus = Literal["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"]

SimTime = Annotated[datetime, BeforeValidator(parse_sim_time)]
FuelMap = dict[FuelType, float]


class _Sim(BaseModel):
    """Simulator payloads: ignore unknown fields, but enforce the ones we rely on (malformed data guard)."""

    model_config = ConfigDict(extra="ignore")


class Health(_Sim):
    status: str
    database: str | None = None
    simulation: dict[str, Any] = Field(default_factory=dict)


class Instance(_Sim):
    id: int | None = None
    scenario_id: str
    scenario_version: str | None = None
    seed: int | None = None
    sim_time: SimTime
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    status: SimStatus


class Region(_Sim):
    id: str
    name: str | None = None
    demand_factor: float


class Depot(_Sim):
    id: str
    name: str | None = None
    region_id: str
    status: DepotStatus
    dispatch_capacity_per_tick: float = Field(ge=0)
    capacity: FuelMap
    inventory: FuelMap


class Station(_Sim):
    id: str
    name: str | None = None
    region_id: str
    status: StationStatus
    demand_profile: str
    demand_multiplier: float = Field(ge=0)
    capacity: FuelMap
    inventory: FuelMap


class Route(_Sim):
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = Field(ge=0)
    max_shipment: float = Field(gt=0)
    status: RouteStatus


class SupplyArrival(_Sim):
    id: str
    depot_id: str
    fuel_type: FuelType
    quantity: float = Field(ge=0)
    planned_tick: int
    actual_tick: int | None = None
    status: SupplyStatus


class SimEvent(_Sim):
    id: int
    type: str
    start_tick: int
    end_tick: int
    status: EventStatus
    parameters: dict[str, Any] = Field(default_factory=dict)


class Allocation(_Sim):
    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float
    created_tick: int | None = None
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: AllocationStatus
    failure_reason: str | None = None


class DemandRow(_Sim):
    id: int
    station_id: str
    fuel_type: FuelType
    tick: int
    sim_time: SimTime
    demand_liters: float
    served_liters: float
    unmet_liters: float


class Metrics(_Sim):
    served_demand_liters: float
    unmet_demand_liters: float
    service_level: float
    allocation_liters: float
    allocation_failures: int


class AuditRow(_Sim):
    id: int
    action: str
    tick: int | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    result: str | None = None
    metadata_json: dict[str, Any] | None = None


class AllocationRequest(BaseModel):
    """Body of ``POST /v1/allocations``. Validated client-side so a 422 is a bug, not a runtime event."""

    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float = Field(gt=0)


# --- client envelope -------------------------------------------------------------------

T = TypeVar("T")


@dataclass(frozen=True)
class SimResponse[T]:
    data: T
    stale: bool
    status: int
    latency_ms: float
    tick_hint: int | None = None


class SimErrorKind(StrEnum):
    FAULT = "FAULT"
    DOMAIN = "DOMAIN"
    VALIDATION = "VALIDATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SimError:
    code: str
    message: str
    kind: SimErrorKind
    status: int = 0


class SimulatorState(StrEnum):
    UP = "UP"
    DEGRADED = "DEGRADED"  # /v1/* failing while /v1/health answers -> an injected fault is active
    DOWN = "DOWN"  # /v1/health unreachable too


# --- SSE ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SSEEvent:
    name: str
    data: Any
    raw: str = ""


# --- bus events -----------------------------------------------------------------------


class BusEventType:
    """Names of events on the Redis stream ``fsp:events``."""

    STATE_UPDATED = "state.updated"
    TICK = "tick"
    EVENT_CHANGED = "event.changed"
    ALLOCATION_CHANGED = "allocation.changed"
    SIM_FAULT = "sim.fault"
    SIM_RESET = "sim.reset"
    DECISIONS_UPDATED = "decisions.updated"
    ALERT = "alert"
    HEALTH = "health"


class BusMessage(BaseModel):
    id: str = ""
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = 0.0
