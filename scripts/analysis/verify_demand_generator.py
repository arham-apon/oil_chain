"""Spec §10.1: empirically verify the simulator's demand generator against the documented formula.

Resets the simulator (admin endpoints - test/analysis use only), injects two demand spikes, steps N ticks and
compares ``demand_liters`` with

    baseline(s,f,t) = daily[profile][f] * (tick_minutes/1440) * hour_factor(profile, hour) * region_factor * mult

Usage:  python scripts/analysis/verify_demand_generator.py [--ticks 480] [--url http://localhost:8000] [--out file.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict

import numpy as np

from fsp_shared import world_constants as wc
from fsp_shared.sim_client import SimClient
from fsp_shared.timeutil import hour_of_day

SPIKES = [  # (start_tick, duration, parameters)
    (300, 24, {"station_ids": ["station-tongi"], "multiplier": 1.8}),
    (400, 12, {"region_ids": ["region-chattogram"], "multiplier": 1.5}),
]


async def collect(url: str, ticks: int):
    c = SimClient(url, 10.0)
    await c.admin_pause()
    await c.admin_reset()
    await c.admin_pause()
    for start, dur, params in SPIKES:
        if start < ticks:
            await c.admin_create_event("demand_spike", start, dur, params)
    for _ in range(ticks):
        await c.admin_step()
    rows = []
    for sid in wc.STATIONS:
        rows += (await c.demand_history(sid, 2000)).data
    inst = (await c.instance()).data
    events = (await c.events()).data
    await c.aclose()
    return inst, rows, events


def analyse(inst, rows, events):
    tm = inst.tick_minutes
    spike_ticks: dict[str, set[int]] = defaultdict(set)  # station -> ticks covered by any spike
    for e in events:
        p = e.parameters
        for sid, st in wc.STATIONS.items():
            covered = (not p.get("station_ids") and not p.get("region_ids")) or sid in p.get("station_ids", []) or st[
                "region_id"
            ] in p.get("region_ids", [])
            if covered:
                spike_ticks[sid].update(range(e.start_tick, e.end_tick))
    out: dict = {"ticks": inst.tick, "tick_minutes": tm, "rows": len(rows), "hour_table": {}, "noise": {}}

    # implied hour factor per (profile, hour): mean(demand) / (daily * tm/1440 * region_factor), outside spikes
    implied: dict[tuple[str, int], list[float]] = defaultdict(list)
    ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        st = wc.STATIONS[r.station_id]
        prof = st["demand_profile"]
        rf = wc.REGIONS[st["region_id"]]["demand_factor"]
        if r.tick in spike_ticks[r.station_id] or r.tick <= 0:
            continue
        base_no_hour = wc.DEMAND_PROFILES[prof]["daily"][r.fuel_type] * tm / 1440.0 * rf
        h = hour_of_day(r.sim_time)
        implied[(prof, h)].append(r.demand_liters / base_no_hour)
        ratios[(r.station_id, r.fuel_type)].append(r.demand_liters / wc.baseline_demand(prof, r.fuel_type, h, tm, rf))
    for prof in wc.DEMAND_PROFILES:
        out["hour_table"][prof] = {
            h: round(float(np.mean(implied[(prof, h)])), 3) for h in range(24) if implied.get((prof, h))
        }
    for (sid, fuel), rs in sorted(ratios.items()):
        prof = wc.STATIONS[sid]["demand_profile"]
        out["noise"][f"{sid}/{fuel}"] = {
            "n": len(rs),
            "mean_ratio": round(float(np.mean(rs)), 4),
            "std_ratio": round(float(np.std(rs)), 4),
            "documented_noise": wc.DEMAND_PROFILES[prof]["noise"],
        }

    # spike alignment: ratio to the *no-multiplier* baseline around each spike edge (station-tongi @300, 24 ticks)
    edge = {}
    by = {(r.station_id, r.fuel_type, r.tick): r for r in rows}
    for label, sid, lo, hi, _mult in (("tongi", "station-tongi", 296, 328, 1.8), ("coxsbazar", "station-coxsbazar", 396, 416, 1.5)):
        st = wc.STATIONS[sid]
        seq = {}
        for t in range(lo, hi):
            rs = []
            for f in wc.FUEL_TYPES:
                r = by.get((sid, f, t))
                if r is None:
                    continue
                h = hour_of_day(r.sim_time)
                rf = wc.REGIONS[st["region_id"]]["demand_factor"]
                rs.append(r.demand_liters / wc.baseline_demand(st["demand_profile"], f, h, tm, rf))
            if rs:
                seq[t] = round(float(np.mean(rs)), 2)
        edge[label] = seq
    out["spike_edges"] = edge

    # sim_time <-> tick convention
    sample = next(r for r in rows if r.tick == 12)
    out["sim_time_at_tick_12"] = sample.sim_time.isoformat()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=480)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--out")
    a = ap.parse_args()
    inst, rows, events = asyncio.run(collect(a.url, a.ticks))
    res = analyse(inst, rows, events)
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=2, default=str)
    json.dump(res, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    main()
    
