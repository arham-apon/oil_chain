"""Load test of the platform's critical paths (spec §16). NEVER mutates the simulator: only dry-run / read endpoints.

    locust -f tests/load/locustfile.py --headless -u 50 -r 0.83 -t 5m --csv docs/data/loadtest

Hosts are configurable via env: DECISION_URL, FORECAST_URL, GATEWAY_URL (defaults: localhost dev ports).
"""

import os
import random

from locust import FastHttpUser, between, task

DECISION = os.environ.get("DECISION_URL", "http://localhost:18103")
FORECAST = os.environ.get("FORECAST_URL", "http://localhost:18102")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")

PAIRS = [("station-mirpur", "route-gazipur-mirpur"), ("station-mirpur", "route-patiya-mirpur"),
         ("station-tongi", "route-gazipur-tongi"), ("station-karnaphuli", "route-patiya-karnaphuli"),
         ("station-karnaphuli", "route-gazipur-karnaphuli"), ("station-coxsbazar", "route-patiya-coxsbazar")]
FUELS = ["DIESEL", "PETROL", "OCTANE"]


class Operator(FastHttpUser):
    host = GATEWAY
    wait_time = between(0.2, 1.0)

    @task(4)
    def whatif(self):  # Decision API: pure computation, never POSTs to the simulator
        station, route = random.choice(PAIRS)
        self.client.post(f"{DECISION}/whatif", name="decision POST /whatif",
                         json={"station_id": station, "fuel_type": random.choice(FUELS),
                               "qty": random.choice([1000, 2500, 4000]), "route_id": route})

    @task(1)
    def dry_run_cycle(self):  # full planning cycle (forecast + MILP + validate + triage), dry_run=true
        self.client.post(f"{DECISION}/cycle/run?dry_run=true", name="decision POST /cycle/run?dry_run")

    @task(4)
    def forecast(self):  # Prediction API
        self.client.get(f"{FORECAST}/forecast", name="forecast GET /forecast (12 pairs)")

    @task(3)
    def overview(self):
        self.client.get("/api/overview", name="gateway GET /api/overview")

    @task(2)
    def decisions(self):
        self.client.get("/api/decisions", name="gateway GET /api/decisions")
