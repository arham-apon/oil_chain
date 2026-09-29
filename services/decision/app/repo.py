"""Persistence helpers for decisions, alerts, audit and AI-call logs."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fsp_shared.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger("decision.repo")

DECISION_COLS = (
    "decision_id::text AS decision_id, idempotency_key, cycle_tick, station_id, fuel_type, source_depot_id, route_id, "
    "quantity, system_origin, status, facts, jev, explanation, sim_allocation_id, sim_error_code, operator, "
    "operator_note, created_at, updated_at"
)


class Repo:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    # --- decisions ---------------------------------------------------------------------------------------------------
    async def insert_decision(self, *, cycle_tick: int, station_id: str, fuel: str, depot_id: str, route_id: str,
                              qty: float, origin: str, status: str, facts: dict, jev: dict | None,
                              operator: str | None = None, note: str | None = None,
                              decision_id: uuid.UUID | None = None) -> str:
        did = decision_id or uuid.uuid4()
        async with self.engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO decisions (decision_id, idempotency_key, cycle_tick, station_id, fuel_type, source_depot_id,"
                    " route_id, quantity, system_origin, status, facts, jev, operator, operator_note) VALUES (:id, :key,"
                    " :t, :s, :f, :d, :r, :q, :o, :st, CAST(:facts AS jsonb), CAST(:jev AS jsonb), :op, :note)"
                ),
                {"id": did, "key": f"fsp-{did}", "t": cycle_tick, "s": station_id, "f": fuel, "d": depot_id,
                 "r": route_id, "q": qty, "o": origin, "st": status, "facts": json.dumps(facts, default=str),
                 "jev": json.dumps(jev, default=str) if jev is not None else None, "op": operator, "note": note},
            )
        return str(did)

    async def update_decision(self, decision_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, params = [], {"id": decision_id}
        for k, v in fields.items():
            if k in ("facts", "jev", "explanation"):
                sets.append(f"{k} = CAST(:{k} AS jsonb)")
                params[k] = json.dumps(v, default=str) if v is not None else None
            else:
                sets.append(f"{k} = :{k}")
                params[k] = v
        async with self.engine.begin() as c:
            await c.execute(
                text(f"UPDATE decisions SET {', '.join(sets)}, updated_at = now() WHERE decision_id = CAST(:id AS uuid)"),
                params,
            )

    async def transition(self, decision_id: str, from_statuses: tuple[str, ...], to_status: str, **fields: Any) -> bool:
        """Atomic compare-and-set on status (prevents double execution / double approval)."""
        params = {"id": decision_id, "to": to_status, "from": list(from_statuses)}
        extra = ""
        for k, v in fields.items():
            extra += f", {k} = :{k}"
            params[k] = v
        async with self.engine.begin() as c:
            res = await c.execute(
                text(
                    f"UPDATE decisions SET status = :to, updated_at = now(){extra} "
                    "WHERE decision_id = CAST(:id AS uuid) AND status = ANY(:from)"
                ),
                params,
            )
        return (res.rowcount or 0) == 1

    async def get_decision(self, decision_id: str) -> dict | None:
        async with self.engine.connect() as c:
            row = (await c.execute(text(f"SELECT {DECISION_COLS} FROM decisions WHERE decision_id = CAST(:id AS uuid)"),
                                   {"id": decision_id})).mappings().first()
        return dict(row) if row else None

    async def list_decisions(self, status: str | None = None, limit: int = 100) -> list[dict]:
        q = f"SELECT {DECISION_COLS} FROM decisions"
        params: dict[str, Any] = {"limit": limit}
        if status:
            q += " WHERE status = ANY(:st)"
            params["st"] = status.split(",")
        q += " ORDER BY created_at DESC LIMIT :limit"
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(text(q), params)).mappings().all()]

    async def expire_staged(self, current_tick: int, ttl: int) -> int:
        async with self.engine.begin() as c:
            res = await c.execute(
                text("UPDATE decisions SET status = 'EXPIRED', updated_at = now() "
                     "WHERE status = 'STAGED_REVIEW' AND cycle_tick < :cut"),
                {"cut": current_tick - ttl},
            )
        return res.rowcount or 0

    async def by_status(self, *statuses: str) -> list[dict]:
        return await self.list_decisions(",".join(statuses), limit=500)

    # --- alerts / audit / ai calls ------------------------------------------------------------------------------------------
    async def alert(self, *, tick: int | None, kind: str, severity: str, message: str, entity_id: str | None = None,
                    fuel: str | None = None, data: dict | None = None) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                text("INSERT INTO alerts (tick, kind, severity, entity_id, fuel_type, message, data) "
                     "VALUES (:t, :k, :s, :e, :f, :m, CAST(:d AS jsonb))"),
                {"t": tick, "k": kind, "s": severity, "e": entity_id, "f": fuel, "m": message,
                 "d": json.dumps(data or {}, default=str)},
            )

    async def audit(self, *, tick: int | None, actor: str, action: str, entity_type: str, entity_id: str | None,
                    result: str, data: dict | None = None) -> None:
        try:
            async with self.engine.begin() as c:
                await c.execute(
                    text("INSERT INTO audit_log (tick, actor, action, entity_type, entity_id, result, data) "
                         "VALUES (:t, :a, :ac, :et, :e, :r, CAST(:d AS jsonb))"),
                    {"t": tick, "a": actor, "ac": action, "et": entity_type, "e": entity_id, "r": result,
                     "d": json.dumps(data or {}, default=str)},
                )
        except Exception as exc:  # noqa: BLE001 - auditing must not break the decision path
            log.error("audit_failed", action=action, error=str(exc)[:200])

    async def ai_call(self, provider: str, purpose: str, latency_ms: int, ok: bool, error: str | None) -> None:
        try:
            async with self.engine.begin() as c:
                await c.execute(
                    text("INSERT INTO ai_calls (provider, purpose, latency_ms, ok, error) VALUES (:p, :pu, :l, :ok, :e)"),
                    {"p": provider, "pu": purpose, "l": latency_ms, "ok": ok, "e": error},
                )
        except Exception:  # noqa: BLE001
            pass

    async def recent_alert_keys(self, since_tick: int) -> set[tuple]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT kind, entity_id, fuel_type FROM alerts WHERE tick >= :t"),
                                    {"t": since_tick})).all()
        return {(r[0], r[1], r[2]) for r in rows}
