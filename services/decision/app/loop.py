"""Decision cycle (spec §11.1): load -> expire -> detect -> forecast -> plan -> validate -> triage -> gate -> execute ->
cancel -> publish. Single-flight: a trigger that arrives while a cycle runs is skipped, never queued."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fsp_shared import world_constants as wc
from fsp_shared.breaker import BreakerState, CircuitBreaker
from fsp_shared.config import Settings
from fsp_shared.logging import bind_context, get_logger
from fsp_shared.risk import compute_risk
from fsp_shared.schemas import BusEventType, HorizonPoint, PairForecast
from fsp_shared.sim_client import SimClient
from fsp_shared.timeutil import ticks_per_hour

from . import metrics as m
from .cancel import cancel_doomed
from .detect import AlertCandidate, AnomalyTracker, detect
from .executor import Executor
from .forecasts import ForecastClient, arrivals_for
from .planner.context import PairCtx, Proposal, build_context
from .planner.heuristic import plan_heuristic
from .planner.milp import solve_milp
from .repo import Repo
from .triage.jev import JevTriage, Triage
from .triage.policy import gate
from .validator import Candidate, shrink_and_validate, validate
from .world import AllocView, ReservedView, World, load_world

log = get_logger("decision.loop")
ALERT_DEDUP_TICKS = 16


@dataclass
class CycleResult:
    tick: int | None
    dry_run: bool
    planner: str = "NONE"
    forecast_source: str = "NONE"
    proposals: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    duration_ms: float = 0.0
    skipped: str | None = None


def build_facts(world: World, p: Proposal, c: PairCtx, pf: PairForecast, alternatives: list[dict]) -> dict[str, Any]:
    """Numbers computed by code (spec §12.2) — the LLM may only narrate these."""
    tph = ticks_per_hour(world.tick_minutes)
    route = world.routes[p.route_id]
    arr = arrivals_for(world, p.station_id, p.fuel) + [
        (world.tick + 1 + world.routes[r.route_id].transit_ticks, r.qty) for r in world.reserved
        if r.station_id == p.station_id and r.fuel == p.fuel and r.route_id in world.routes
    ]
    after = compute_risk(c.inventory, pf.horizon, world.tick, world.tick_minutes,
                         arr + [(world.tick + 1 + route.transit_ticks, p.qty)])
    return {
        "alert_entity_id": p.station_id,
        "fuel_type": p.fuel,
        "tick": world.tick,
        "sim_time": world.sim_time.isoformat(),
        "projected_stockout_hours": None if c.risk.t_empty_ticks is None else round(c.risk.t_empty_ticks / tph, 2),
        "time_to_empty_ticks": c.risk.t_empty_ticks,
        "current_inventory_liters": round(c.inventory, 1),
        "tank_capacity_liters": c.capacity,
        "in_flight_liters": round(c.in_flight, 1),
        "expected_demand_liters": round(sum(pt.mean for pt in pf.horizon), 1),
        "horizon_ticks": len(pf.horizon),
        "burn_rate_liters_per_hour": round(c.risk.burn_rate_lph, 1),
        "recommended_allocation_liters": p.qty,
        "source_depot_id": p.depot_id,
        "transit_route_id": p.route_id,
        "transit_ticks": route.transit_ticks,
        "stockout_risk_before": round(c.risk.stockout_risk, 3),
        "stockout_risk_after": round(after.stockout_risk, 3),
        "target_level_liters": round(c.target, 1),
        "need_liters": round(c.need, 1),
        "priority_weight": c.weight,
        "binding_constraints": p.binding_constraints,
        "confidence": {"forecast_sigma": round(pf.residual_sigma, 2), "forecast_source": pf.source,
                       "model_version": pf.model_version},
        "alternatives": alternatives,
        "single_route_station": p.station_id in wc.SINGLE_ROUTE_STATIONS,
        "planner": p.origin,
        "simulated_data": True,
    }


def alternatives_for(world: World, p: Proposal, c: PairCtx, pf: PairForecast, derate: float) -> list[dict]:
    out = []
    for r in c.routes:
        if r.id == p.route_id:
            continue
        cand, code = shrink_and_validate(world, Candidate(r.depot_id, p.station_id, r.id, p.fuel, p.qty), derate=derate)
        entry: dict[str, Any] = {"route_id": r.id, "depot_id": r.depot_id, "transit_ticks": r.transit_ticks,
                                 "validator": code}
        if cand is not None:
            risk = compute_risk(c.inventory, pf.horizon, world.tick, world.tick_minutes,
                                arrivals_for(world, p.station_id, p.fuel) + [(world.tick + 1 + r.transit_ticks, cand.qty)])
            entry.update({"feasible_qty": cand.qty, "stockout_risk_after": round(risk.stockout_risk, 3)})
        out.append(entry)
    if not out:
        out.append({"route_id": None, "note": "no alternate path: this station is reachable by a single route"})
    return out


def jev_state(world: World, p: Proposal, c: PairCtx, facts: dict) -> dict[str, Any]:
    route = world.routes[p.route_id]
    active = sorted({f"{e.type}:{e.status}" for e in world.events if e.status in ("ACTIVE", "SCHEDULED")})
    return {
        "station": p.station_id, "fuel_type": p.fuel, "current_inventory_liters": round(c.inventory, 1),
        "tank_capacity_liters": c.capacity, "in_flight_liters": round(c.in_flight, 1),
        "station_available_ullage": round(max(0.0, 0.98 * c.capacity - c.inventory - c.in_flight), 1),
        "burn_rate_liters_per_hour": facts["burn_rate_liters_per_hour"], "time_to_empty_ticks": c.risk.t_empty_ticks,
        "stockout_risk": round(c.risk.stockout_risk, 3), "demand_multiplier": world.stations[p.station_id].multiplier,
        "active_events": active, "route_status": route.status,
        "depot_inventory_liters": round(world.depots[p.depot_id].inventory[p.fuel], 1),
        "proposed_dispatch": {"qty": p.qty, "route_id": p.route_id, "depot_id": p.depot_id,
                              "transit_ticks": route.transit_ticks},
        "route_max_shipment": route.max_shipment, "data_stale": world.stale,
    }


class DecisionEngine:
    def __init__(self, settings: Settings, engine, client: SimClient, repo: Repo, forecast: ForecastClient,
                 jev: JevTriage, jev_breaker: CircuitBreaker, bus=None) -> None:
        self.s = settings
        self.engine = engine
        self.client = client
        self.repo = repo
        self.forecast = forecast
        self.jev = jev
        self.jev_breaker = jev_breaker
        self.bus = bus
        self.executor = Executor(client, repo, settings.DEPOT_CONSTRAINT_DERATE)
        self.anomaly = AnomalyTracker()
        self.lock = asyncio.Lock()
        self.last: CycleResult | None = None
        self.last_forecast_source = "NONE"
        self.last_planner = "NONE"
        self.last_world: World | None = None
        self.last_forecasts: dict = {}
        self.sim_fault_stale = False
        self._alert_seen: dict[tuple, int] = {}
        self._http = httpx.AsyncClient(timeout=5.0)
        self._bg: set[asyncio.Task] = set()

    # ----------------------------------------------------------------------------------------------------------------
    def mode(self) -> str:
        if (self.last_world and self.last_world.stale) or self.sim_fault_stale:
            return "DEGRADED"
        if self.last_forecast_source == "BASELINE_FALLBACK" or self.jev_breaker.state is BreakerState.OPEN \
                or self.last_planner == "HEURISTIC" or (self.jev.enabled is False and self.s.TYPESAFE_API_KEY):
            return "FALLBACK"
        return "NORMAL"

    def reset(self) -> None:
        self.anomaly = AnomalyTracker()
        self._alert_seen.clear()
        self.jev.cache.clear()
        self.last_world = None

    def _bg_task(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _publish(self, event_type: str, payload: dict) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - Redis down must not break decisions
            pass

    async def request_explanation(self, decision_id: str) -> None:
        async def go() -> None:
            try:
                await self._http.post(f"{self.s.COGNITIVE_URL}/explain", json={"decision_id": decision_id}, timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - explanations are additive; decision center works without them
                log.info("explain_request_failed", error=str(exc)[:120])

        self._bg_task(go())

    async def request_incident(self, sim_event_id: int | None, reason: str) -> None:
        async def go() -> None:
            try:
                await self._http.post(f"{self.s.COGNITIVE_URL}/incident",
                                      json={"sim_event_id": sim_event_id, "reason": reason}, timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                log.info("incident_request_failed", error=str(exc)[:120])

        self._bg_task(go())

    # ----------------------------------------------------------------------------------------------------------------
    async def run_cycle(self, *, dry_run: bool = False, reason: str = "tick") -> CycleResult:
        if self.lock.locked():
            m.CYCLES_SKIPPED.inc()
            return CycleResult(None, dry_run, skipped="cycle already running")
        async with self.lock:
            started = time.perf_counter()
            try:
                res = await self._cycle(dry_run, reason)
            finally:
                m.DECISION_CYCLE.observe(time.perf_counter() - started)
            res.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if not dry_run:
                self.last = res
            m.MODE.set({"NORMAL": 0, "DEGRADED": 1, "FALLBACK": 2}[self.mode()])
            return res

    async def _cycle(self, dry_run: bool, reason: str) -> CycleResult:
        world = await load_world(self.engine)
        if world is None or not world.stations or not world.routes:
            return CycleResult(None, dry_run, skipped="no simulator state yet")
        bind_context(tick=world.tick)
        res = CycleResult(world.tick, dry_run)
        stale = world.stale or self.sim_fault_stale
        s = self.s

        # 2 auto-approve unreviewed staged proposals (review window elapsed), then expire the rest
        if not dry_run and not stale and s.STAGED_AUTO_APPROVE_AFTER_TICKS > 0:
            for d in await self.repo.by_status("STAGED_REVIEW"):
                if world.tick - d["cycle_tick"] < s.STAGED_AUTO_APPROVE_AFTER_TICKS:
                    continue
                if await self.repo.transition(d["decision_id"], ("STAGED_REVIEW",), "AUTO_APPROVED",
                                              operator="auto-approve-timeout"):
                    await self.repo.audit(tick=world.tick, actor="decision-svc", action="decision.auto_approve_timeout",
                                          entity_type="decision", entity_id=d["decision_id"], result="OK",
                                          data={"waited_ticks": world.tick - d["cycle_tick"]})
                    d = await self.repo.get_decision(d["decision_id"])
                    await self.executor.execute(d, world)  # pre-flight re-validates on fresh state
            world = await load_world(self.engine) or world
        if not dry_run:
            n = await self.repo.expire_staged(world.tick, s.STAGED_DECISION_TTL_TICKS)
            if n:
                world = await load_world(self.engine) or world  # expired reservations no longer hold stock

        # 4 forecast
        forecasts, fsource = await self.forecast.get(world, s.HORIZON_TICKS)
        res.forecast_source = fsource
        ctxs = build_context(world, forecasts)
        for (sid, fuel), c in ctxs.items():
            m.STOCKOUT_RISK.labels(sid, fuel).set(c.risk.stockout_risk)

        # 3 detect
        cands = detect(world, forecasts, ctxs) + await self._anomalies(forecasts)
        if not dry_run:
            await self._raise_alerts(world, cands)
        res.alerts = [a.__dict__ for a in cands]

        # 5 plan
        planner = "MILP"
        try:
            proposals = await asyncio.to_thread(solve_milp, world, ctxs, forecasts, s)
        except Exception as exc:  # noqa: BLE001 - solver error/timeout/infeasible -> heuristic
            log.warning("milp_failed_using_heuristic", error=str(exc)[:200])
            planner = "HEURISTIC"
            proposals = plan_heuristic(world, ctxs, s)
        if fsource == "BASELINE_FALLBACK" and planner == "MILP":
            pass  # MILP on baseline forecasts is still the optimiser; origin is set to HEURISTIC_FALLBACK by the gate
        m.PLANNER_USED.labels(planner).inc()
        res.planner = planner

        # 6 pre-flight validate (sequentially, each accepted proposal reserves its quantity for the next)
        accepted: list[tuple[Proposal, PairCtx]] = []
        for p in proposals:
            cand, code = shrink_and_validate(world, Candidate(p.depot_id, p.station_id, p.route_id, p.fuel, p.qty),
                                             derate=s.DEPOT_CONSTRAINT_DERATE)
            if cand is None or cand.qty < s.MIN_LOT_LITERS:
                res.dropped.append({"route_id": p.route_id, "station_id": p.station_id, "fuel": p.fuel, "qty": p.qty,
                                    "code": code})
                continue
            p.qty = cand.qty
            world.reserved.append(ReservedView(f"plan-{len(accepted)}", p.depot_id, p.station_id, p.route_id, p.fuel,
                                               p.qty, "PROPOSED"))
            accepted.append((p, ctxs[(p.station_id, p.fuel)]))
        # remove the planning placeholders; real reservations are added as decisions are created
        world.reserved = [r for r in world.reserved if not r.decision_id.startswith("plan-")]

        # 7 triage + 8 gate
        facts_list = []
        for p, c in accepted:
            pf = forecasts[(p.station_id, p.fuel)]
            facts = build_facts(world, p, c, pf, alternatives_for(world, p, c, pf, s.DEPOT_CONSTRAINT_DERATE))
            facts_list.append(facts)
        triages: list[Triage] = await asyncio.gather(
            *(self.jev.triage(jev_state(world, p, c, f), world.tick) for (p, c), f in zip(accepted, facts_list, strict=True))
        )
        severed = False
        for (p, c), facts, tri in zip(accepted, facts_list, triages, strict=True):
            g = gate(tri, data_stale=stale, jev_breaker=self.jev_breaker.state, jev_enabled=self.jev.enabled,
                     forecast_source=fsource, planner=planner, stockout_risk=c.risk.stockout_risk,
                     noul_min=s.AUTO_APPROVE_NOUL_MIN, urgency_max=s.AUTO_APPROVE_URGENCY_MAX)
            if g.status == "AUTO_APPROVED" and not s.AUTONOMOUS_DISPATCH:
                g = type(g)("STAGED_REVIEW", g.origin, "autonomous dispatch disabled (AUTONOMOUS_DISPATCH=false)")
            facts["gate_reason"] = g.reason
            entry = {"station_id": p.station_id, "fuel": p.fuel, "route_id": p.route_id, "depot_id": p.depot_id,
                     "qty": p.qty, "status": g.status, "origin": g.origin, "gate_reason": g.reason,
                     "jev": tri.to_json(), "facts": facts}
            res.proposals.append(entry)
            severed |= tri.crisis_class == "bottleneck_severed"
            if dry_run:
                continue
            did = await self.repo.insert_decision(cycle_tick=world.tick, station_id=p.station_id, fuel=p.fuel,
                                                  depot_id=p.depot_id, route_id=p.route_id, qty=p.qty, origin=g.origin,
                                                  status=g.status, facts=facts, jev=tri.to_json())
            entry["decision_id"] = did
            m.DECISIONS.labels(g.origin, g.status).inc()
            world.reserved.append(ReservedView(did, p.depot_id, p.station_id, p.route_id, p.fuel, p.qty, g.status))
            if tri.crisis_class == "upstream_starvation":
                await self.repo.alert(tick=world.tick, kind="UPSTREAM_STARVATION", severity="HIGH",
                                      message=f"Jev: {p.depot_id} reserves too low to resupply {p.station_id}",
                                      entity_id=p.depot_id, fuel=p.fuel)

        # 9 execute auto-approved; staged ones get an explanation (non-blocking)
        if not dry_run:
            for entry in res.proposals:
                if entry["status"] == "STAGED_REVIEW":
                    await self.request_explanation(entry["decision_id"])
                    continue
                d = await self.repo.get_decision(entry["decision_id"])
                if d is None:
                    continue
                out = await self.executor.execute(d, world)
                entry["result"] = out
                if out == "COMMITTED":
                    world.reserved = [r for r in world.reserved if r.decision_id != d["decision_id"]]
                    world.allocations.append(AllocView(-1, d["idempotency_key"], d["source_depot_id"], d["station_id"],
                                                       d["route_id"], d["fuel_type"], float(d["quantity"]), world.tick,
                                                       None, None, "PENDING"))
                elif out == "SIM_REJECTED":
                    world.reserved = [r for r in world.reserved if r.decision_id != d["decision_id"]]
            # 10 cancel doomed PENDING allocations
            res.cancelled = await cancel_doomed(world, self.client, self.repo)
            await self._publish(BusEventType.DECISIONS_UPDATED, {"tick": world.tick, "count": len(res.proposals),
                                                                 "mode": self.mode()})
            if severed:
                await self.request_incident(None, "Jev classified a proposal as bottleneck_severed")

        self.last_forecast_source, self.last_planner = fsource, planner
        self.last_world, self.last_forecasts = world, forecasts
        return res

    async def _anomalies(self, forecasts) -> list[AlertCandidate]:
        """Feed the latest observed demand per pair to the (observed - forecast)/sigma > 3 tracker."""
        from sqlalchemy import text

        try:
            async with self.engine.connect() as c:
                rows = (await c.execute(text(
                    "SELECT DISTINCT ON (station_id, fuel_type) station_id, fuel_type, tick, demand_liters "
                    "FROM demand_observations ORDER BY station_id, fuel_type, tick DESC"))).all()
        except Exception:  # noqa: BLE001
            return []
        observed = {(r[0], r[1]): (int(r[2]), float(r[3] or 0.0)) for r in rows}
        return self.anomaly.update(observed, forecasts)

    async def _raise_alerts(self, world: World, cands: list[AlertCandidate]) -> None:
        for a in cands:
            key = (a.kind, a.entity_id, a.fuel)
            last = self._alert_seen.get(key)
            if last is not None and world.tick - last < ALERT_DEDUP_TICKS and world.tick >= last:
                continue
            self._alert_seen[key] = world.tick
            m.ALERTS.labels(a.kind).inc()
            await self.repo.alert(tick=world.tick, kind=a.kind, severity=a.severity, message=a.message,
                                  entity_id=a.entity_id, fuel=a.fuel, data=a.data)
            await self._publish(BusEventType.ALERT, {"tick": world.tick, "kind": a.kind, "severity": a.severity,
                                                     "message": a.message, "entity_id": a.entity_id})

    # ----------------------------------------------------------------------------------------------------------------
    async def approve(self, decision_id: str, *, operator: str, note: str | None, qty: float | None,
                      route_id: str | None) -> dict:
        d = await self.repo.get_decision(decision_id)
        if d is None:
            raise KeyError(decision_id)
        if d["status"] != "STAGED_REVIEW":
            raise ValueError(f"decision is {d['status']}, not STAGED_REVIEW")
        world = await load_world(self.engine)
        edited = (qty is not None and abs(qty - d["quantity"]) > 1e-6) or (route_id and route_id != d["route_id"])
        new_route = route_id or d["route_id"]
        new_qty = float(qty if qty is not None else d["quantity"])
        route = world.routes.get(new_route) if world else None
        if route is None or route.station_id != d["station_id"]:
            raise ValueError(f"route {new_route} does not serve {d['station_id']}")
        code = validate(world, Candidate(route.depot_id, d["station_id"], new_route, d["fuel_type"], new_qty,
                                         exclude_decision_id=decision_id), derate=self.s.DEPOT_CONSTRAINT_DERATE)
        if code != "OK":
            raise ValueError(f"pre-flight failed on fresh state: {code}")
        origin = "SYSTEM2_OVERRIDE" if edited else d["system_origin"]
        if not await self.repo.transition(decision_id, ("STAGED_REVIEW",), "OPERATOR_APPROVED", operator=operator,
                                          operator_note=note, quantity=new_qty, route_id=new_route,
                                          source_depot_id=route.depot_id, system_origin=origin):
            raise ValueError("decision changed concurrently")
        if edited:
            m.OVERRIDES.inc()
        await self.repo.audit(tick=world.tick, actor=operator, action="decision.approve" + ("_with_edits" if edited else ""),
                              entity_type="decision", entity_id=decision_id, result="OK",
                              data={"qty": new_qty, "route_id": new_route, "note": note})
        d = await self.repo.get_decision(decision_id)
        out = await self.executor.execute(d, world)
        await self._publish(BusEventType.DECISIONS_UPDATED, {"tick": world.tick, "decision_id": decision_id})
        return {**(await self.repo.get_decision(decision_id)), "result": out}

    async def reject(self, decision_id: str, *, operator: str, note: str | None) -> dict:
        if not await self.repo.transition(decision_id, ("STAGED_REVIEW",), "REJECTED", operator=operator,
                                          operator_note=note):
            raise ValueError("only STAGED_REVIEW decisions can be rejected")
        await self.repo.audit(tick=None, actor=operator, action="decision.reject", entity_type="decision",
                              entity_id=decision_id, result="OK", data={"note": note})
        await self._publish(BusEventType.DECISIONS_UPDATED, {"decision_id": decision_id})
        return await self.repo.get_decision(decision_id)

    async def manual(self, *, station_id: str, fuel: str, qty: float, route_id: str, operator: str, note: str | None,
                     staged: bool = False, origin: str = "OPERATOR_MANUAL") -> dict:
        world = await load_world(self.engine)
        if world is None:
            raise ValueError("no simulator state yet")
        route = world.routes.get(route_id)
        if route is None or route.station_id != station_id:
            raise ValueError(f"route {route_id} does not serve {station_id}")
        code = validate(world, Candidate(route.depot_id, station_id, route_id, fuel, qty), derate=self.s.DEPOT_CONSTRAINT_DERATE)
        if code != "OK" and not staged:
            raise ValueError(f"pre-flight failed: {code}")
        forecasts, _ = await self.forecast.get(world, self.s.HORIZON_TICKS)
        pf = forecasts.get((station_id, fuel))
        inv = world.stations[station_id].inventory[fuel]
        before = compute_risk(inv, pf.horizon if pf else [HorizonPoint(tick=world.tick + 1, mean=0, sigma=0)],
                              world.tick, world.tick_minutes, arrivals_for(world, station_id, fuel))
        after = compute_risk(inv, pf.horizon if pf else [HorizonPoint(tick=world.tick + 1, mean=0, sigma=0)],
                             world.tick, world.tick_minutes,
                             arrivals_for(world, station_id, fuel) + [(world.tick + 1 + route.transit_ticks, qty)])
        facts = {"alert_entity_id": station_id, "fuel_type": fuel, "tick": world.tick,
                 "current_inventory_liters": inv, "recommended_allocation_liters": qty,
                 "source_depot_id": route.depot_id, "transit_route_id": route_id, "transit_ticks": route.transit_ticks,
                 "stockout_risk_before": round(before.stockout_risk, 3), "stockout_risk_after": round(after.stockout_risk, 3),
                 "time_to_empty_ticks": before.t_empty_ticks, "validator": code, "binding_constraints": [],
                 "alternatives": [], "simulated_data": True, "created_by": operator}
        status = "STAGED_REVIEW" if staged else "OPERATOR_APPROVED"
        did = await self.repo.insert_decision(cycle_tick=world.tick, station_id=station_id, fuel=fuel,
                                              depot_id=route.depot_id, route_id=route_id, qty=qty, origin=origin,
                                              status=status, facts=facts, jev=None, operator=operator, note=note)
        m.DECISIONS.labels(origin, status).inc()
        await self.repo.audit(tick=world.tick, actor=operator, action="decision.manual" + (".staged" if staged else ""),
                              entity_type="decision", entity_id=did, result=code, data={"qty": qty, "route_id": route_id})
        if staged:
            await self.request_explanation(did)
            return await self.repo.get_decision(did)
        d = await self.repo.get_decision(did)
        out = await self.executor.execute(d, world)
        return {**(await self.repo.get_decision(did)), "result": out}

    async def aclose(self) -> None:
        await self._http.aclose()
