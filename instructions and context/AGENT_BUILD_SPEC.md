# AGENT BUILD SPEC — Intelligent Fuel Supply Operations Platform
### BUP CSE FEST 2026 · Hackathon Finals · Build instructions for an autonomous coding agent

> **You are the coding agent.** This file is your single source of instructions. Build the platform phase by phase, in order. Do not skip acceptance checks. Do not move to the next phase until the current phase's **Acceptance** list passes. When something here is ambiguous, apply the **Source Authority Order** (§0.2) and write your decision into `docs/DECISIONS.md`.

---

## 0. Operating Rules for the Agent

### 0.1 Non-negotiable rules
1. **Never modify the simulator.** It runs from the published image `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`. Judges run your submission against that image.
2. **The only domain write is `POST /v1/allocations`** (plus `POST /v1/allocations/{id}/cancel`). Everything else on `/v1/*` is read-only.
3. **REST is the source of truth. SSE is only a hint** that something changed. After any interesting SSE event, re-GET the affected resources.
4. **LLMs never generate fuel volumes, routes, or numbers used for dispatch.** Volumes come from the optimizer (PuLP) or the deterministic heuristic. Gemini only explains, summarizes, and answers questions using tool outputs.
5. **No secrets in the repo.** All keys and passwords come from `.env` (git-ignored). Ship `.env.example` with placeholders only.
6. **Everything must start with one command:** `docker compose up`.
7. **Never run a load test that POSTs real allocations to the simulator.** Load tests use dry-run endpoints.
8. **Label all data in the UI as simulated.** Never connect to real infrastructure or real payment systems.
9. **Keep humans in the loop for consequential actions** exactly as the gating policy in §9.5 defines.
10. **Verify third-party library APIs against the installed version** before writing code that depends on them (run `python -c "import pkg; help(pkg.X)"` or read the installed source). If an API in this spec does not exist in the installed version, adapt to the real API and record it in `docs/DECISIONS.md`.

### 0.2 Source Authority Order (highest wins on conflict)
1. **Official Simulator Integration & Interaction Guide** — ground truth for endpoints, payloads, validation order, world data, events, faults, status codes.
2. **Official participant brief** (as documented in "Intelligent Fuel Supply Operations Platform" doc) — ground truth for requirements, deliverables, rubric, demo storyline, guardrails.
3. **TypeSafe Jev documentation** (the Jev developer guide) — ground truth for Jev SDK usage.
4. **AI-generated plans** ("Fuel Operations Backend Architecture", "Fuel Supply AI Integration Plan") — architecture choices, formulas, targets. Use them, but they contain errors listed in §1.

### 0.3 User's required stack
- **Backend:** Python 3.12, FastAPI, microservices (preferred).
- **Database:** PostgreSQL 16.
- **ML:** scikit-learn (Ridge regression pipeline).
- **Optimization:** PuLP (CBC solver).
- **AI:** TypeSafe **Jev** (System 1 fast typed judgments) + **Google Gemini via LangChain** (System 2 explanation + copilot). Cloud APIs only, **no local models**.
- **Messaging:** Redis (streams / pub-sub).
- **Frontend:** React + TypeScript + Vite.
- **Observability:** Prometheus + Grafana + cAdvisor, structured JSON logs.
- **Load testing:** Locust.
- **CI:** GitHub Actions.

---

## 1. Corrections — Read Before Writing Any Code

The AI-generated plans contain these errors. **Use the corrected version.**

| # | What the AI plans say | Correct behavior (from official sources) |
|---|---|---|
| C1 | `station-tongi` starts with **1,000 L** diesel and runs out in ~4 ticks | Official §8.3: Tongi starts with **11,000 L** diesel. There is no startup emergency. Discard the "4.42 ticks" analysis. |
| C2 | After `route-gazipur-tongi` is disrupted, reroute Tongi "from Patiya via Chattogram" | **No such route exists.** Tongi is reachable **only** by `route-gazipur-tongi`; Cox's Bazar **only** by `route-patiya-coxsbazar`. Rerouting is possible only for **Mirpur** (`gazipur-mirpur` 2 ticks / `patiya-mirpur` 4 ticks) and **Karnaphuli** (`patiya-karnaphuli` 2 / `gazipur-karnaphuli` 4). For single-route stations the correct response is: pre-stock before a scheduled disruption, show "no alternate path" in the UI, and prioritize refill when the route returns. |
| C3 | "Dijkstra rerouting" | The network has direct depot→station routes only. No multi-hop. "Rerouting" = choosing another direct route. |
| C4 | Idempotent replay returns 200 or 201 | §5.4 says replay returns **201**; the cheat sheet lists 200. **Treat both 200 and 201 as success.** |
| C5 | `DESTINATION_CAPACITY_EXCEEDED` if inventory + **in-flight** + qty > capacity | Simulator checks `inventory + quantity > capacity` (no in-flight). **Our planner is stricter:** it also counts in-flight so tanks never overflow on arrival. |
| C6 | Jev urgency returns 1.0–5.0 | Jev `Score` is **0-indexed** and probability-weighted: with 5 levels it returns 0.0–4.0. Compute `urgency_1to5 = answer.score + 1`. |
| C7 | Use `langchain_typesafe.TypeSafeClassifier`, `response.scores[...]`, `response.nouls[...]` | Not in the TypeSafe docs. Use the documented **`typesafe-sdk`**: `AsyncTypeSafeClient().system_one(state=..., questions=...)` and `response.answers["key"]`. Check first with `pip index versions langchain-typesafe`; if it exists and is documented, you may wrap Jev as a LangChain Runnable, but the core call uses `typesafe-sdk`. |
| C8 | Noul has a confidence field | **Noul returns only `.noul` (0–1).** `Choice` and `Score` return `.confidence` and `.probabilities`. |
| C9 | Jev fits inside a 125 ms tick budget | Jev takes 70–300 ms, Gemini ~1–3 s. At 8 ticks/s you **cannot** call AI every tick. Decouple: the decision loop runs on its own cadence (§9.1), never blocks ingestion, and skips ticks gracefully. |
| C10 | Resilience = 20% of score | Official rubric: **Resilience & Incident Response = 10%** (§17). |
| C11 | docker-compose hardcodes `production_secure_pass` | Use `${POSTGRES_PASSWORD}` from `.env`. Zero hardcoded secrets is graded. |
| C12 | `simulate_contingency_flow` copilot tool returns hardcoded numbers | Must call the real decision-service what-if endpoint (§9.7). |
| C13 | MILP objective `weight * (target_fill - alloc_in)` and holding term `C·(Cap − I)` | First term has no `max(0,·)` (use a slack variable). Holding term is constant w.r.t. decision vars (does nothing). Use the formulation in §9.3. |
| C14 | Sample MILP uses walrus `(d := ...)` inside comprehensions | Shadows variables; rewrite cleanly with explicit index maps. |
| C15 | Sample simulator client is sync, retries without jitter | Write an async client per §6. |
| C16 | One error body format | Three formats: faults `unavailable`/`error_rate` → `{"error":{"code":"FAULT_INJECTED",...}}`; `stream_disconnect` and allocation errors → `{"detail":{"code":...}}`; Pydantic 422 → `{"detail":[...]}`. Parse all three. |
| C17 | `idempotency_key UUID` DB column | Simulator allows strings of length 1–150. Use `TEXT`. |
| C18 | Features `multiplier`, `is_event` read from demand history | `/v1/demand-history` has no such columns. Join them from our own station snapshots and event records by tick. |
| C19 | `burn_rate_lph = 4 × per-tick demand` | Only true for 15-min ticks. Use `ticks_per_hour = 60 / tick_minutes` with `tick_minutes` read from `/v1/instance`. |
| C20 | Brief lists `GET /stations` with coordinates, routes with distance | Conceptual. Real endpoints are `/v1/*`, and there are **no coordinates or distances**. Use a static display-coordinates config for the map, marked "approximate, display only". |
| C21 | Brief's example constraint "driver hours" | Not modeled by the simulator. Constraints are exactly those in the validation order (§5.3). Do not invent others. |
| C22 | `gemini-2.0-flash` model | Make it `GEMINI_MODEL` env var with that default. At startup, make one test call; if the model is unavailable, set it to a currently available Gemini Flash model and document it in README. |
| C23 | Official guide §2 bullet "Crisis events and faults are" is truncated | Interpret as: events and faults can only be injected via `/admin/*`. Our platform only reads them via `/v1/events`. |

---

## 2. What We Are Building

A resilient **Fuel Supply Operations Platform** on top of the simulator that runs the closed loop:

**Observe → Detect → Predict → Decide → Simulate → Act → Monitor → Recover**

### 2.1 Required deliverables (official brief §6.1) — all must exist at the end
1. Working application (UI + backend).
2. Source repository with env config files, dependency definitions, setup docs.
3. Verifiable bi-directional simulator integration (reads REST/SSE, writes allocations).
4. Intelligence component (forecasting + optimization + heuristic + AI triage/explanation).
5. Operator interface showing operational state and allocations.
6. Architecture diagram (ingestion, intelligence, decision engines, UI, observability).
7. Deployment package (`docker compose up`).
8. Observability evidence (metrics dashboards, structured logs, health checks).
9. Resilience demonstration (graceful degradation on component failure).
10. Load-test evidence (latency distributions, throughput, saturation).
11. Final live demo runbook (14 steps, §16).

A Jupyter notebook alone is **not** acceptable.

---

## 3. Simulator Reference (condensed from the official guide)

### 3.1 Runtime
- Base URL from our containers: `http://simulator-api:8000` (host: `http://localhost:8000`).
- Swagger `/docs`, ReDoc `/redoc`, admin console `/admin`.
- Env: `SIMULATION_SPEED` (ticks/s wall-clock, default 8), `TICK_MINUTES` (default 15), `SIMULATOR_START_MODE` (`paused` default | `running`).
- Deterministic: same scenario + seed + actions + injected events ⇒ identical state. Baseline scenario has **no preloaded events**. Seed 12345. Start time `2026-01-01T00:00:00+00:00`.

### 3.2 Read endpoints (`/v1/*`, all subject to faults except `/v1/health`)
| Endpoint | Notes |
|---|---|
| `GET /v1/health` | Liveness, **bypasses faults**. `{status, database, simulation:{status,tick}}` |
| `GET /v1/instance` | `tick`, `sim_time`, `tick_minutes`, `status` (PAUSED/RUNNING), `seed`, `scenario_id` |
| `GET /v1/regions` | `demand_factor` (Dhaka 1.00, Chattogram 1.08) |
| `GET /v1/depots`, `/v1/depots/{id}` | `status` OPEN/CONSTRAINED, `dispatch_capacity_per_tick`, `capacity{}`, `inventory{}` |
| `GET /v1/stations`, `/v1/stations/{id}` | `status` OPEN/OUTAGE, `demand_profile`, `demand_multiplier` (changed by demand_spike), `capacity{}`, `inventory{}` |
| `GET /v1/routes` | `transit_ticks`, `max_shipment`, `status` AVAILABLE/DISRUPTED |
| `GET /v1/supply-arrivals` | sorted by `planned_tick`; `status` SCHEDULED/DELAYED/ARRIVED; `actual_tick` |
| `GET /v1/events` | id-desc; `type`, `start_tick`, `end_tick`, `status` SCHEDULED/ACTIVE/RESOLVED, `parameters` |
| `GET /v1/allocations` | id-desc; status PENDING/IN_TRANSIT/ARRIVED/FAILED/CANCELLED; `failure_reason` |
| `GET /v1/demand-history?station_id=&limit=` | limit clamped [1,2000], default 200; 12 rows/tick total (4 stations × 3 fuels); fields `tick, sim_time, demand_liters, served_liters, unmet_liters`. Table grows forever — always pass `limit`. |
| `GET /v1/metrics` | `served_demand_liters, unmet_demand_liters, service_level, allocation_liters, allocation_failures` |
| `GET /v1/stream` | SSE (§7) |

### 3.3 World data (fixed)
**Depots**
| id | region | dispatch/tick | capacity D/P/O | initial D/P/O |
|---|---|---|---|---|
| depot-gazipur | region-dhaka | 12,000 | 90,000/70,000/45,000 | 60,000/45,000/26,000 |
| depot-patiya | region-chattogram | 11,000 | 85,000/65,000/40,000 | 55,000/42,000/24,000 |

**Stations**
| id | region | profile | capacity D/P/O | initial D/P/O |
|---|---|---|---|---|
| station-mirpur | dhaka | urban_high | 15,000/14,000/9,000 | 9,000/9,000/5,000 |
| station-tongi | dhaka | industrial | 18,000/9,000/6,000 | 11,000/6,000/3,500 |
| station-karnaphuli | chattogram | highway | 14,000/15,000/9,000 | 8,500/9,500/5,200 |
| station-coxsbazar | chattogram | regional | 12,000/12,000/7,000 | 7,500/7,500/4,200 |

**Routes**
| id | depot → station | transit_ticks | max_shipment |
|---|---|---|---|
| route-gazipur-mirpur | gazipur → mirpur | 2 | 7,000 |
| route-gazipur-tongi | gazipur → tongi | 2 | 6,500 |
| route-patiya-karnaphuli | patiya → karnaphuli | 2 | 7,000 |
| route-patiya-coxsbazar | patiya → coxsbazar | 3 | 6,000 |
| route-gazipur-karnaphuli | gazipur → karnaphuli | 4 | 5,000 |
| route-patiya-mirpur | patiya → mirpur | 4 | 5,000 |

**Demand profiles (L/simulated day)** — DIESEL/PETROL/OCTANE, noise
- urban_high 8,500/10,500/5,600, 0.10
- industrial 14,000/4,500/2,200, 0.08
- highway 10,500/11,000/6,200, 0.12
- regional 7,200/7,600/3,600, 0.10

**Hour-of-day factors**
- industrial: 06:00–17:59 → 1.55, else 0.45
- highway: 06–09 or 16–20 → 1.35, else 0.75
- urban_high: 07–09 or 16–20 → 1.45, else 0.70
- regional: 07:00–20:59 → 1.25, 21:00–06:59 → 0.65

(For highway/urban_high the hour boundaries are ambiguous — confirm empirically in Phase 3.)

**Supply arrivals:** 22 total — 4 initial-burst arrivals at ticks 12–20, then 18 recurring arrivals every 64 ticks (~16 h), sized to about one day of regional demand.

Store all of the above in `shared/world_constants.py` **only as fallback/cold-start data**. At runtime always read the live values from REST (capacities, routes, statuses can be read; the demand tables are not exposed by the API, so they stay as constants).

### 3.4 Allocation write
`POST /v1/allocations` body:
```json
{"idempotency_key":"fsp-<uuid>","source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","route_id":"route-gazipur-mirpur","fuel_type":"DIESEL","quantity":3000}
```
- `idempotency_key` string 1–150; `fuel_type` DIESEL|PETROL|OCTANE; `quantity` > 0 and ≤ `route.max_shipment`.
- Lifecycle: created `PENDING` at `created_tick` → departs next tick (`IN_TRANSIT`) → arrives at `departure_tick + transit_ticks`. Official example: created 5, departed 6, arrived 8.
- A route that is DISRUPTED **at departure time** makes the allocation `FAILED` (counts in `allocation_failures`).
- Same key + same body → returns existing (201). Same key + different body → 409 `IDEMPOTENCY_KEY_MISMATCH`. **Cancellation does not free the key.**
- `POST /v1/allocations/{id}/cancel` → only for PENDING; refunds depot inventory. 404 `ALLOCATION_NOT_FOUND`, 409 `CANNOT_CANCEL`.

### 3.5 Validation order (first failure wins) — replicate exactly in our pre-flight validator
1. Idempotency check
2. `NOT_FOUND` (404) — unknown depot/station/route
3. `ROUTE_MISMATCH` (409) — route endpoints ≠ request
4. `DEPOT_CLOSED` (409) — depot.status ∉ {OPEN, CONSTRAINED}
5. `STATION_CLOSED` (409) — station.status ≠ OPEN
6. `ROUTE_DISRUPTED` (409) — route.status ≠ AVAILABLE
7. `ROUTE_CAPACITY_EXCEEDED` (409) — quantity > max_shipment
8. `INSUFFICIENT_INVENTORY` (409) — depot.inventory[fuel] < quantity
9. `DISPATCH_CAPACITY_EXCEEDED` (409) — (in-flight + pending from this depot on this tick) + quantity > dispatch_capacity_per_tick
10. `DESTINATION_CAPACITY_EXCEEDED` (409) — station.inventory[fuel] + quantity > station.capacity[fuel]

### 3.6 Events (injected by organizers via `/admin/events`; we read them via `/v1/events`)
| type | params | effect while ACTIVE | on resolve |
|---|---|---|---|
| demand_spike | multiplier (1.5), station_ids[], region_ids[] | station demand_multiplier ×= multiplier | ÷ multiplier |
| route_disruption | route_ids[] | routes → DISRUPTED | → AVAILABLE |
| station_outage | station_ids[] | station → OUTAGE, served = 0 | → OPEN |
| depot_constraint | depot_ids[] | depot → CONSTRAINED (still shippable, signals reduced capacity) | → OPEN |
| shipment_delay | delay_ticks (2), depot_ids[], fuel_types[] | supply planned_tick += delay, status DELAYED | one-shot, not undone |
| supply_shortfall | factor (0.5), depot_ids[], fuel_types[] | supply quantity ×= factor | one-shot, not restored |

Empty filter lists mean "all entities". `end_tick = start_tick + duration_ticks`. **SCHEDULED events are visible before they start — use them to plan ahead.**

### 3.7 Faults (organizers inject via `/admin/faults`; affect `/v1/*` except `/v1/health`)
| type | effect |
|---|---|
| latency `{delay_ms:500}` | every request sleeps delay_ms |
| unavailable | 503 `{"error":{"code":"FAULT_INJECTED",...}}` |
| error_rate `{rate:0.25}` | random 503 with that probability |
| stale_data | requests succeed but GETs carry header `X-Simulator-Stale: true` |
| stream_disconnect | `GET /v1/stream` → 503 `{"detail":{"code":"FAULT_INJECTED"}}` |

Faults auto-expire (duration ≤ 3600 s).

### 3.8 Admin endpoints (bypass faults; for tests and the demo only)
`POST /admin/run | /admin/pause | /admin/toggle | /admin/step | /admin/reset | /admin/events | /admin/faults | /admin/faults/clear`, `GET /admin/audit?limit=`, `GET /admin/faults`, `GET /admin/events`. **Our production decision path must not depend on admin endpoints.** Use them in tests, chaos scripts, and optional demo controls behind `DEMO_CONTROLS=true`.

---

## 4. Architecture

### 4.1 Services
```
                          ┌────────────────────────────┐
                          │  simulator-api (published) │
                          │  REST /v1/*  SSE /v1/stream│
                          └───────▲───────────┬────────┘
                    POST /v1/allocations      │ GET + SSE
                                  │           ▼
┌──────────────┐   Redis    ┌─────┴──────────────────┐
│ ingestion-svc│──streams──►│ decision-svc           │
│ SSE + REST   │            │ planner (PuLP MILP)    │──HTTP──► forecast-svc (scikit-learn)
│ sync → PG    │            │ pre-flight validator   │           ▲ killable → heuristic
└──────┬───────┘            │ Jev triage (System 1)  │
       │                    │ (s,S) heuristic        │──HTTP──► cognitive-svc
       ▼                    │ executor + outbox      │          (LangChain + Gemini, System 2)
┌──────────────┐            └─────────┬──────────────┘
│ PostgreSQL 16│◄─────────────────────┘
└──────┬───────┘
       ▼
┌──────────────┐  WebSocket  ┌──────────────┐
│ gateway-svc  │────────────►│ frontend     │
│ FastAPI BFF  │◄────REST────│ React (Vite) │
└──────────────┘             └──────────────┘
Prometheus ◄── /metrics from all services;  Grafana dashboards;  cAdvisor for CPU/RAM
```

| Service | Port | Responsibility |
|---|---|---|
| `ingestion-svc` | 8101 | Owns the SSE connection and all REST reads. Normalizes and persists snapshots to Postgres. Publishes `state.updated`, `tick`, `event.changed`, `allocation.changed`, `sim.fault` to Redis. Tracks simulator health and stale flag. |
| `forecast-svc` | 8102 | scikit-learn Ridge demand models: training, prediction, stockout risk. **Killable for the demo** — when it is down, decision-svc falls back to the heuristic. |
| `decision-svc` | 8103 | The brain. Decision loop, planner, validator, Jev triage, heuristic fallback, circuit breakers, executor with outbox + idempotency, cancellation, what-if simulation. |
| `cognitive-svc` | 8104 | Gemini via LangChain: structured explanations, incident briefs, copilot agent with read-only tools. |
| `gateway-svc` | 8080 | Backend-for-frontend: REST + WebSocket for the UI, operator approve/reject/override, aggregated health console, optional demo controls. |
| `frontend` | 5173 (dev) / 3000 (prod) | Operator UI. |
| `postgres` | 5432 | Persistence. |
| `redis` | 6379 | Internal event bus. |
| `prometheus` / `grafana` / `cadvisor` | 9090 / 3001 / 8081 | Observability. |

**Why this split:** the official brief requires graceful degradation when the "ML model service" or a dependency is down. Separate `forecast-svc` and `cognitive-svc` let the demo kill them while `decision-svc` keeps dispatching.

### 4.2 Data flow per decision cycle
1. `ingestion-svc` receives `simulation.tick` → debounced REST sync → persists snapshot for tick *t* → publishes `state.updated{tick}`.
2. `decision-svc` (single-flight) loads the latest snapshot from Postgres, asks `forecast-svc` for horizons (breaker-protected), computes risk/time-to-empty, runs the MILP, pre-flight validates, triages with Jev (breaker-protected), gates each proposal.
3. Auto-approved → executor POSTs with idempotency key → persists result.
   Staged → `cognitive-svc` builds explanation → gateway pushes to UI → operator approves/edits/rejects → executor POSTs.
4. Everything is logged, measured, audited.

---

## 5. Repository Layout

```
fuel-ops-platform/
├── docker-compose.yml
├── docker-compose.override.yml      # dev hot-reload (optional)
├── .env.example
├── .gitignore                       # includes .env
├── README.md
├── Makefile                         # up, down, test, lint, loadtest, chaos, demo-reset
├── docs/
│   ├── ARCHITECTURE.md              # Mermaid diagrams + explanation
│   ├── architecture.png             # exported diagram
│   ├── DECISIONS.md                 # every deviation / assumption
│   ├── API.md                       # our service APIs
│   ├── RESILIENCE_REPORT.md
│   ├── LOADTEST_REPORT.md
│   ├── MODEL_REPORT.md              # forecast accuracy vs baselines
│   └── DEMO_RUNBOOK.md
├── shared/                          # installed as a local package in every service image
│   ├── pyproject.toml
│   └── fsp_shared/
│       ├── config.py                # pydantic-settings
│       ├── world_constants.py       # §3.3 tables (fallback only)
│       ├── db.py                    # async SQLAlchemy engine/session
│       ├── models.py                # ORM tables (§8)
│       ├── schemas.py               # Pydantic DTOs shared between services
│       ├── sim_client.py            # defensive async simulator client (§6)
│       ├── sse.py                   # SSE parser (§7)
│       ├── breaker.py               # circuit breaker (§11.2)
│       ├── bus.py                   # Redis streams helpers
│       ├── logging.py               # structlog JSON config
│       ├── metrics.py               # common Prometheus metrics + FastAPI middleware
│       └── timeutil.py              # tick ↔ hour-of-day helpers
├── services/
│   ├── ingestion/  (Dockerfile, pyproject.toml, app/main.py, app/sync.py, app/stream_worker.py, app/demand_backfill.py, tests/)
│   ├── forecast/   (app/main.py, app/features.py, app/model.py, app/trainer.py, app/risk.py, tests/)
│   ├── decision/   (app/main.py, app/loop.py, app/state.py, app/planner/milp.py, app/planner/heuristic.py,
│   │                app/validator.py, app/triage/jev.py, app/triage/policy.py, app/executor.py,
│   │                app/cancel.py, app/detect.py, app/whatif.py, tests/)
│   ├── cognitive/  (app/main.py, app/schemas.py, app/explain.py, app/incident.py, app/copilot.py, app/tools.py, tests/)
│   └── gateway/    (app/main.py, app/routers/{state,decisions,copilot,health,audit,demo}.py, app/ws.py, tests/)
├── frontend/       (Vite React TS app)
├── infra/
│   ├── prometheus/prometheus.yml
│   ├── grafana/provisioning/{datasources,dashboards}/ + dashboards/*.json
│   └── db/migrations/               # Alembic
├── scripts/
│   ├── chaos/                       # fault-injection scripts (§14)
│   ├── demo/                        # demo step scripts (§16)
│   └── wait_for.py
├── tests/
│   ├── integration/test_simulator_pipeline.py
│   └── load/locustfile.py
└── .github/workflows/ci.yml
```

---

## 6. Phase 0 — Bootstrap

### Tasks
1. Create the repo layout above (empty modules with docstrings are fine).
2. `.env.example`:
   ```dotenv
   # Simulator
   SIMULATOR_URL=http://simulator-api:8000
   SIMULATION_SPEED=8
   TICK_MINUTES=15
   SIMULATOR_START_MODE=paused
   # Postgres
   POSTGRES_DB=fuel_platform
   POSTGRES_USER=fuel_admin
   POSTGRES_PASSWORD=change-me
   DATABASE_URL=postgresql+asyncpg://fuel_admin:change-me@postgres:5432/fuel_platform
   REDIS_URL=redis://redis:6379/0
   # AI (cloud only)
   TYPESAFE_API_KEY=
   GEMINI_API_KEY=
   GEMINI_MODEL=gemini-2.0-flash
   JEV_MODEL=jev-latest
   # Decision policy
   DECISION_INTERVAL_TICKS=4
   AUTO_APPROVE_NOUL_MIN=0.85
   AUTO_APPROVE_URGENCY_MAX=4.5
   STAGED_DECISION_TTL_TICKS=16
   MIN_LOT_LITERS=500
   ROUTE_CAP_MODE=per_route_per_cycle      # or per_allocation
   DEPOT_CONSTRAINT_DERATE=0.5
   JEV_TIMEOUT_MS=600
   GEMINI_TIMEOUT_MS=3000
   BREAKER_FAILURE_THRESHOLD=5
   BREAKER_OPEN_SECONDS=30
   SIM_HTTP_TIMEOUT_S=3.0
   DEMO_CONTROLS=true
   GRAFANA_ADMIN_PASSWORD=change-me
   ```
3. `docker-compose.yml`: simulator-api (image pinned, env from `.env`, port 8000), postgres:16-alpine with healthcheck `pg_isready`, redis:7-alpine, the five services (build from `services/<name>`, depend on postgres healthy + redis started + simulator started), frontend, prometheus, grafana (provisioned), cadvisor. One bridge network `fuel-net`. Named volume `pgdata`. No `version:` key. Every app service has a Docker healthcheck calling its own `/health` using Python (`python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:PORT/health')"`) since curl may be absent.
4. Each Python service: `pyproject.toml`, multi-stage Dockerfile (python:3.12-slim), non-root user, installs `shared/` as a package. Generate a lock file (`uv lock` or `pip-compile`) and **pin the versions you actually installed**.
5. Dependencies (verify availability, then pin): `fastapi uvicorn[standard] httpx pydantic pydantic-settings sqlalchemy[asyncio] asyncpg alembic redis structlog prometheus-client tenacity numpy pandas scikit-learn joblib pulp typesafe-sdk langchain langchain-core langchain-google-genai pytest pytest-asyncio respx locust ruff mypy`.
6. Makefile targets: `up`, `down`, `logs`, `test`, `lint`, `migrate`, `loadtest`, `chaos-<fault>`, `demo-reset`.

### Acceptance
- `docker compose up -d` starts everything; `curl localhost:8000/v1/health` returns `status: ok`; each service `/health` returns 200.
- `git grep -nE "(API_KEY|PASSWORD)=.+[A-Za-z0-9]"` finds nothing except `.env.example` placeholders.

---

## 7. Phase 1 — Shared Library

### 7.1 Config (`config.py`)
Pydantic `BaseSettings` reading every variable in `.env.example`. No defaults for secrets.

### 7.2 Defensive async simulator client (`sim_client.py`)
Requirements:
- `httpx.AsyncClient(base_url=SIMULATOR_URL, timeout=SIM_HTTP_TIMEOUT_S)`, one shared instance per service.
- Typed methods for every `/v1/*` endpoint in §3.2 returning Pydantic models, plus `post_allocation`, `cancel_allocation`, and admin methods (used only by tests/demo).
- Every response returns `SimResponse[T](data, stale: bool, status, latency_ms, tick_hint)`. `stale = headers.get("X-Simulator-Stale") == "true"`.
- **Retry policy (GET):** retry on 503, timeouts, connection errors; exponential backoff with full jitter (base 100 ms, cap 2 s, max 4 attempts). Do **not** retry 4xx.
- **Retry policy (POST allocation):** same retryable set, **the same body and same idempotency_key on every attempt** (max 5). Never change the body on retry.
- **Error parsing:** implement `parse_sim_error(resp) -> SimError(code, message, kind)` handling `{"error":{...}}`, `{"detail":{...}}`, and `{"detail":[...]}` (C16). `kind ∈ {FAULT, DOMAIN, VALIDATION, UNKNOWN}`.
- Metrics per call: `sim_client_requests_total{endpoint,method,status}`, `sim_client_latency_seconds{endpoint}` histogram, `sim_faults_detected_total{type}` (fault/stale/latency>1s).
- If `/v1/*` calls fail but `/v1/health` succeeds → simulator state = `DEGRADED` (fault active). If health fails too → `DOWN`.

### 7.3 SSE parser (`sse.py`)
Implement your own parser over `client.stream("GET", "/v1/stream")`:
- Lines starting with `:` are comments (`: connected`, `: keepalive`) → update `last_seen` only.
- `event: <name>` + `data: <json>` + blank line → yield `SSEEvent(name, data)`.
- Read timeout **45 s** (keepalive arrives every 15 s of silence; 15 s silence is normal).
- A 503 on connect = `stream_disconnect` fault → raise `StreamFaultError`.

### 7.4 Circuit breaker (`breaker.py`)
States CLOSED → OPEN → HALF_OPEN. Opens after `BREAKER_FAILURE_THRESHOLD` consecutive failures **or** calls exceeding the timeout; stays OPEN `BREAKER_OPEN_SECONDS`; HALF_OPEN allows one probe. Exposes `breaker_state{dependency}` gauge (0 closed, 1 half-open, 2 open) and `fallback_activations_total{dependency,reason}`. Used for `jev`, `gemini`, `forecast`.

### 7.5 Other
- `logging.py`: structlog JSON with fields `service, level, event, tick, decision_id, idempotency_key, trace_id`.
- `metrics.py`: FastAPI middleware producing `http_requests_total{service,route,method,status}` and `http_request_duration_seconds{service,route}` histogram; `/metrics` endpoint.
- `timeutil.py`: `hour_of_day(sim_time)` (parse `sim_time` string, do not assume tick math), `ticks_per_hour(tick_minutes)`.
- `bus.py`: Redis Streams `XADD fsp:events` with `MAXLEN ~ 10000`; consumer groups per service.

### Acceptance (unit tests with `respx`)
- Stale header detected. All three error formats parsed. POST retries keep identical body/key. 4xx not retried.
- SSE parser handles comments, multi-event chunks, keepalive, and 503.
- Breaker opens after threshold, recovers via half-open.

---

## 8. Phase 2 — Database Schema (Alembic migration `0001`)

Use `fuel_type` naming (matches simulator). All timestamps `TIMESTAMPTZ`.

```sql
CREATE TABLE sim_ticks (
  tick INT PRIMARY KEY, sim_time TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
  tick_minutes INT NOT NULL, stale BOOLEAN NOT NULL DEFAULT false,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE depot_snapshots (
  id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, depot_id TEXT NOT NULL, fuel_type TEXT NOT NULL,
  inventory DOUBLE PRECISION NOT NULL, capacity DOUBLE PRECISION NOT NULL,
  dispatch_capacity_per_tick DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, stale BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (depot_id, fuel_type, tick));
CREATE INDEX ON depot_snapshots (depot_id, tick DESC);

CREATE TABLE station_snapshots (
  id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, station_id TEXT NOT NULL, fuel_type TEXT NOT NULL,
  inventory DOUBLE PRECISION NOT NULL, capacity DOUBLE PRECISION NOT NULL,
  demand_multiplier DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, stale BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (station_id, fuel_type, tick));
CREATE INDEX ON station_snapshots (station_id, tick DESC);

CREATE TABLE route_snapshots (
  id BIGSERIAL PRIMARY KEY, tick INT NOT NULL, route_id TEXT NOT NULL, status TEXT NOT NULL,
  transit_ticks INT NOT NULL, max_shipment DOUBLE PRECISION NOT NULL, UNIQUE (route_id, tick));

CREATE TABLE supply_arrivals (          -- upsert by id
  id TEXT PRIMARY KEY, depot_id TEXT, fuel_type TEXT, quantity DOUBLE PRECISION,
  planned_tick INT, actual_tick INT, status TEXT, updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE sim_events (               -- upsert by id
  id INT PRIMARY KEY, type TEXT, start_tick INT, end_tick INT, status TEXT,
  parameters JSONB, first_seen_tick INT, updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE demand_observations (
  id BIGINT PRIMARY KEY,                -- simulator row id
  station_id TEXT NOT NULL, fuel_type TEXT NOT NULL, tick INT NOT NULL, sim_time TIMESTAMPTZ NOT NULL,
  demand_liters DOUBLE PRECISION, served_liters DOUBLE PRECISION, unmet_liters DOUBLE PRECISION,
  UNIQUE (station_id, fuel_type, tick));
CREATE INDEX ON demand_observations (station_id, fuel_type, tick DESC);

CREATE TABLE sim_allocations (          -- mirror of /v1/allocations, upsert by sim id
  id INT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, source_depot_id TEXT, destination_station_id TEXT,
  route_id TEXT, fuel_type TEXT, quantity DOUBLE PRECISION, created_tick INT, departure_tick INT,
  expected_arrival_tick INT, actual_arrival_tick INT, status TEXT, failure_reason TEXT,
  updated_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX ON sim_allocations (status) WHERE status IN ('PENDING','IN_TRANSIT');

CREATE TABLE sim_metrics (tick INT PRIMARY KEY, served DOUBLE PRECISION, unmet DOUBLE PRECISION,
  service_level DOUBLE PRECISION, allocation_liters DOUBLE PRECISION, allocation_failures INT);

CREATE TABLE forecasts (
  id BIGSERIAL PRIMARY KEY, made_at_tick INT NOT NULL, station_id TEXT, fuel_type TEXT,
  model_version TEXT, horizon JSONB NOT NULL,          -- [{tick, mean, sigma}]
  residual_sigma DOUBLE PRECISION, source TEXT NOT NULL);  -- MODEL | BASELINE_FALLBACK

CREATE TABLE model_registry (
  version TEXT PRIMARY KEY, trained_at_tick INT, n_rows INT, mae DOUBLE PRECISION, rmse DOUBLE PRECISION,
  baseline_mae DOUBLE PRECISION, artifact_path TEXT, active BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE decisions (
  decision_id UUID PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,   -- 'fsp-' || decision_id
  cycle_tick INT NOT NULL, station_id TEXT, fuel_type TEXT, source_depot_id TEXT, route_id TEXT,
  quantity DOUBLE PRECISION NOT NULL,
  system_origin TEXT NOT NULL,     -- SYSTEM1_AUTO | SYSTEM2_OVERRIDE | HEURISTIC_FALLBACK | OPERATOR_MANUAL
  status TEXT NOT NULL,            -- PROPOSED|STAGED_REVIEW|AUTO_APPROVED|OPERATOR_APPROVED|REJECTED|EXPIRED|
                                   -- COMMITTING|COMMITTED|SIM_REJECTED|CANCELLED|SUPERSEDED
  facts JSONB NOT NULL,            -- computed numbers (§9.6)
  jev JSONB,                       -- {urgency_1to5, urgency_conf, urgency_probs, crisis_class, crisis_conf, auto_approve}
  explanation JSONB,               -- Gemini narrative or template
  sim_allocation_id INT, sim_error_code TEXT,
  operator TEXT, operator_note TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX ON decisions (status, cycle_tick DESC);

CREATE TABLE alerts (id BIGSERIAL PRIMARY KEY, tick INT, kind TEXT, severity TEXT, entity_id TEXT,
  fuel_type TEXT, message TEXT, data JSONB, acknowledged BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE incidents (id BIGSERIAL PRIMARY KEY, sim_event_id INT, brief JSONB, source TEXT, created_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE audit_log (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ DEFAULT now(), tick INT, actor TEXT,
  action TEXT, entity_type TEXT, entity_id TEXT, result TEXT, data JSONB);

CREATE TABLE ai_calls (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ DEFAULT now(), provider TEXT, purpose TEXT,
  latency_ms INT, ok BOOLEAN, error TEXT, input_tokens INT, output_tokens INT);
```
Retention job in ingestion-svc: keep snapshots for the last 2,000 ticks (configurable); never delete decisions/audit.

### Acceptance
`alembic upgrade head` runs on container start (ingestion-svc owns migrations; others wait for schema).

---

## 9. Phase 3 — Ingestion Service

### 9.1 Workers (asyncio tasks started in FastAPI lifespan)
1. **Stream worker**
   - Connects to `/v1/stream`. The read loop does **no work**: it only pushes events into a bounded `asyncio.Queue(1000)` and returns to reading (the simulator drops subscribers >200 events behind).
   - On connect/reconnect → trigger a **full sync** (all endpoints) because there is no Last-Event-ID replay.
   - On error/503 → exponential backoff with jitter (0.5 s → 30 s), set `sse_connected=0`, increment `sse_reconnects_total`.
2. **Event processor** — consumes the queue and coalesces:
   - `simulation.tick` → schedule a **debounced tick sync** (at most `SYNC_MAX_HZ=4`; always sync the latest tick).
   - `allocation.status_changed` → upsert allocation from payload **and** mark allocations for re-GET on next sync.
   - `inventory.updated` → mark depots dirty.
   - `simulator.notice` with "Simulation reset" → full resync + publish `sim.reset` (decision-svc clears in-memory state).
3. **Tick sync** — GET instance, depots, stations, routes, events, allocations, metrics (concurrently with `asyncio.gather`). Tag rows with the tick from `/v1/instance` read in the same sync. Upsert into Postgres. Publish `state.updated{tick, stale}`.
4. **Slow sync** — every 8 ticks: supply-arrivals; demand-history per station with `limit = min(2000, 3 × ticks_since_last + 30)`; upsert on `(station_id,fuel_type,tick)`. On startup: **backfill** each station with `limit=2000`.
5. **Poll fallback** — if SSE has been down > 5 s, run tick sync by polling `/v1/instance` every 500 ms so the platform keeps working without SSE.
6. **Health probe** — `/v1/health` every 2 s (bypasses faults) → simulator status UP/DEGRADED/DOWN.

### 9.2 Stale data handling (`X-Simulator-Stale: true`)
- Still store the data but with `stale=true`; keep the last non-stale snapshot as `last_good`.
- Publish `sim.fault{type:"stale_data"}` → decision-svc **suspends auto-commit** (everything stages for review, §9.5) and UI shows a "STALE DATA" banner.
- Keep re-fetching; the first non-stale response clears the flag.

### 9.3 Malformed data guard
Validate every payload with Pydantic. On failure: reject the payload, keep the last valid cache, raise an alert `DATA_INVALID`, increment `sim_payload_invalid_total`.

### 9.4 Endpoints
`GET /health`, `GET /metrics`, `GET /status` (sse state, last tick, last sync latency, stale flag, simulator status).

### Acceptance (integration, simulator in compose)
- With simulator RUNNING, `sim_ticks` advances continuously; snapshots exist per synced tick.
- Inject `stream_disconnect` (60 s): polling fallback keeps `sim_ticks` advancing; after expiry, SSE reconnects and a full sync runs.
- Inject `stale_data`: rows flagged, alert raised, `sim.fault` published.
- Inject `error_rate 0.5`: ingestion keeps up (retries), no crash.
- Demand history has 12 rows per tick with no gaps after backfill.

---

## 10. Phase 4 — Forecast Service (scikit-learn)

### 10.1 Step 1: verify the demand generator (do this first, write results to `docs/MODEL_REPORT.md`)
Hypothesis:
```
baseline(s,f,t) = daily[profile_s][f] × (tick_minutes / 1440) × hour_factor(profile_s, hour(t))
                  × region_factor(region_s) × demand_multiplier(s,t)
```
Pull ≥ 2 simulated days of history; compute `ratio = demand_liters / baseline`. Expect mean ≈ 1.0 and std ≈ the profile noise. Resolve the ambiguous hour windows for highway/urban_high from data. If the hypothesis fails, keep the feature anyway (the model learns the scale) and document the finding.

### 10.2 Training data
- Rows from `demand_observations` joined with `station_snapshots` (for `demand_multiplier`, `status`) on `(station_id, fuel_type, tick)` using forward-fill for missing ticks, and with `sim_events` (for `event_active`).
- Target: **`demand_liters`** (not `served_liters`, which is capped by inventory).
- **Exclude** rows where station status = OUTAGE.

### 10.3 Features (minimum set from the plan, plus two additions)
For station *s*, fuel *f*, tick *t*:
- `sin_hour, cos_hour` where `θ = 2π·hour(t)/24`
- `ewma_4, ewma_16` of past demand (shifted by 1 — no leakage)
- `momentum_4 = d(t-1) − d(t-5)`
- `demand_multiplier(s,t)`, `event_active(s,t)` (0/1)
- **added:** `baseline(s,f,t)` from §10.1, and `hour_factor(profile, hour)`
- One model per (station, fuel): `Pipeline([StandardScaler(), Ridge(alpha=1.0)])`. Tune alpha on a time-ordered validation split (last 20%); never shuffle.

### 10.4 Multi-step forecast
Horizon `H = HORIZON_TICKS` (default 24). Recursive: predict t+1, append, recompute lag features, continue. Future known features: hour, baseline, current multiplier, and **scheduled** `demand_spike` events from `/v1/events` (multiply baseline for ticks inside `[start_tick, end_tick)` if the event targets the station/region). Clamp predictions ≥ 0. Output per tick `{tick, mean, sigma}` with `sigma = residual_sigma` from validation (grow as `sigma·sqrt(k)` for cumulative sums, see §10.5).

### 10.5 Risk metrics (computed here, reused by decision-svc)
- `ticks_per_hour = 60 / tick_minutes`; `burn_rate_lph = mean(next tick) × ticks_per_hour`.
- **Projected inventory** tick by tick: `I(k) = I(0) + arrivals_by_k − Σ_{j≤k} mean_j`, arrivals from our PENDING/IN_TRANSIT allocations (`expected_arrival_tick`, or `created_tick + 1 + transit_ticks` if not yet departed).
- **Time-to-empty** `T_empty` = first k where `I(k) ≤ 0` (∞ if none within H).
- **Stockout risk** within H: `max_k P(C_k > I(0) + arrivals_by_k)` with `C_k ~ Normal(Σ_{j≤k} mean_j, k·σ²)`. Report as probability 0–1.

### 10.6 Cold start & fallback
If fewer than 96 rows or no active model: return `baseline` horizon with `sigma = noise × baseline`, `source = BASELINE_FALLBACK`. decision-svc uses the same baseline function locally when forecast-svc is unreachable.

### 10.7 Training schedule
Background retrain every 96 ticks (one simulated day) and on demand `POST /train`. Register in `model_registry` with MAE/RMSE and **naive baseline MAE** (last-value and baseline-formula). Activate a new model only if it beats the currently active one.

### 10.8 API
- `POST /forecast` `{station_ids?, fuel_types?, horizon?}` → per pair: horizon, burn_rate_lph, T_empty, stockout_risk, residual_sigma, model_version, source.
- `POST /train`, `GET /models`, `GET /health`, `GET /metrics` (`forecast_mae{station,fuel}`, `forecast_inference_seconds`, `forecast_source_total{source}`).

### Acceptance
- Validation MAE beats both naive baselines for every (station, fuel); table in `MODEL_REPORT.md`.
- `/forecast` for all 12 pairs p95 < 30 ms (plan target) after warmup.
- Under an injected demand_spike (1.8×), forecasts adapt within ≤ 4 ticks (multiplier feature).

---

## 11. Phase 5 — Decision Service (the core)

### 11.1 Decision loop (`loop.py`)
- Triggered by `state.updated` from Redis every `DECISION_INTERVAL_TICKS`, and **immediately** on: event changes (route_disruption, demand_spike, station_outage, depot_constraint, shipment_delay, supply_shortfall), allocation FAILED, crisis_class = bottleneck_severed, operator request.
- **Single-flight lock**: if a cycle is still running, skip the trigger (count `decision_cycles_skipped_total`). Never queue a backlog.
- Record `decision_cycle_seconds` histogram.

Cycle steps:
1. **Load state** from Postgres (latest non-stale snapshot; note stale flag).
2. **Expire** STAGED decisions older than `STAGED_DECISION_TTL_TICKS` → `EXPIRED`.
3. **Detect** (§11.8) → alerts.
4. **Forecast** via forecast-svc (breaker `forecast`, timeout 500 ms). On failure → local baseline forecast, `origin=HEURISTIC_FALLBACK` path.
5. **Plan**: MILP (§11.3). On solver error/timeout/infeasible → heuristic (§11.4).
6. **Pre-flight validate** every proposal (§11.5); drop or shrink failing ones.
7. **Triage** with Jev (§11.6) if breaker CLOSED/HALF_OPEN; otherwise deterministic urgency.
8. **Gate** (§11.7) → AUTO_APPROVED or STAGED_REVIEW.
9. **Execute** auto-approved (§11.9); request explanations for staged ones from cognitive-svc (async, non-blocking).
10. **Cancel** doomed PENDING allocations (§11.10).
11. Publish `decisions.updated` to Redis.

### 11.2 Planning inputs
- Available routes per station: `status == AVAILABLE` **and** no SCHEDULED/ACTIVE `route_disruption` covering the departure tick (`current_tick + 1`).
- Stations with status OUTAGE are skipped (would 409 `STATION_CLOSED`) — raise alert.
- Depot usable stock `U(d,f) = inventory − Σ PENDING qty from d for f − reserved (APPROVED not yet committed)`.
- Depot dispatch budget `B(d) = dispatch_capacity_per_tick × (DEPOT_CONSTRAINT_DERATE if CONSTRAINED else 1) − Σ qty of allocations from d with created_tick == current_tick or departure_tick == current_tick (PENDING/IN_TRANSIT)`.
- Station in-flight `F(s,f)` = PENDING + IN_TRANSIT + STAGED/APPROVED-not-committed quantities.
- Lead time for route r: `L_r = 1 + transit_ticks(r)`.
- Target level `T(s,f) = min(0.90 × capacity, forecast demand over (L_r + COVER_TICKS))` where `COVER_TICKS=32`; never below safety stock `ss = mean_per_tick × (L_r + 2)`.
- Need `N(s,f) = max(0, T − projected inventory at arrival)`.
- Priority weight `w(s,f)`: 1000 if `T_empty ≤ L_min + 2` or `stockout_risk ≥ 0.5`; 300 if `stockout_risk ≥ 0.2`; else 100.

### 11.3 MILP formulation (PuLP + CBC, `timeLimit=1s`, `msg=False`)
Variables for each available route r (d→s) and fuel f:
- `x[r,f] ≥ 0` continuous liters
- `y[r,f] ∈ {0,1}` ship-or-not (makes it MILP; enforces minimum lot)
- `u[s,f] ≥ 0` unmet need slack
- `v[d,f] ≥ 0` depot reserve shortfall slack

Objective:
```
min  Σ_{s,f} w(s,f)·u[s,f]
   + Σ_{r,f} c_transit·transit_ticks(r)·x[r,f]          (c_transit = 0.01; prefers short routes)
   + Σ_{d,f} c_reserve·v[d,f]                            (c_reserve = 5; protects depot for its own region)
```
Constraints:
1. Need coverage: `Σ_{r→s} x[r,f] + u[s,f] ≥ N(s,f)`
2. Depot stock: `Σ_{r from d} x[r,f] ≤ U(d,f)`
3. Dispatch budget: `Σ_{r from d, f} x[r,f] ≤ B(d)`
4. Route cap: if `ROUTE_CAP_MODE=per_route_per_cycle` (default, conservative as in plan): `Σ_f x[r,f] ≤ max_shipment(r)`; else per allocation `x[r,f] ≤ max_shipment(r)` (quantities above cap get split into chunks at execution).
5. Min lot / linking: `MIN_LOT·y[r,f] ≤ x[r,f] ≤ max_shipment(r)·y[r,f]`
6. Ullage (stricter than simulator, C5): `inventory(s,f) + F(s,f) + Σ_{r→s} x[r,f] ≤ 0.98·capacity(s,f)`
7. Depot reserve: `U(d,f) − Σ x[r,f] + v[d,f] ≥ R(d,f)` where `R(d,f)` = forecast demand of the depot's **own-region** stations until the next supply arrival for (d,f), × 0.5.

Post-process: floor quantities to multiples of 10 L; drop < MIN_LOT; output `Proposal{route_id, depot, station, fuel, qty, binding_constraints[]}`. Binding constraints = constraints whose slack ≤ 1 L (and PuLP duals for LP relaxation if available) — reused in explanations.

### 11.4 Deterministic (s,S) heuristic fallback (from plan, corrected)
For each (s,f), pick the shortest available route r; `D̄` = forecast mean per tick (or baseline):
```
s_reorder = D̄ × (transit_ticks(r) + 2)
S_target  = 0.90 × capacity(s,f)
if projected_inventory_at_arrival ≤ s_reorder:
    Q = min(S_target − inventory − F(s,f), max_shipment(r), U(d,f), B(d) remaining)
    if Q ≥ MIN_LOT: propose Q
```
Process stations by ascending `T_empty` so the most urgent get budget first. Must run in < 5 ms for 12 pairs. `system_origin = HEURISTIC_FALLBACK`.

### 11.5 Pre-flight validator (`validator.py`)
Replicates §3.5 in the same order on the freshest state, with the simulator's exact conditions (plus our stricter ullage). Returns the first failing code or OK. Unit-test every branch. Proposals that fail are dropped (logged with code); if the only failure is quantity-related, shrink once and re-validate.

### 11.6 Jev triage — System 1 (`triage/jev.py`)
Use the **documented SDK** (C7):
```python
from typesafe_sdk import AsyncTypeSafeClient, Choice, Score, Noul

QUESTIONS = {
  "urgency": Score(
    instructions="Score the inventory depletion urgency of `station` for `fuel_type` using `current_inventory_liters`, `tank_capacity_liters` and `time_to_empty_ticks`.",
    criteria=[
      "Stock above 50% of capacity and time to empty above 24 ticks.",
      "Stock 30-50% of capacity, time to empty between 16 and 24 ticks.",
      "Approaching reorder point, time to empty between 8 and 16 ticks.",
      "High threat, time to empty between 4 and 8 ticks.",
      "Emergency, stockout expected in fewer than 4 ticks.",
    ]),
  "crisis_class": Choice(
    instructions="Which condition is the primary driver of stress at `station`? Use `active_events`, `demand_multiplier`, `depot_inventory_liters` and `route_status`.",
    criteria={
      "nominal": "Normal draw within expected bounds.",
      "transient_surge": "Consumption accelerated by a demand multiplier or spike.",
      "upstream_starvation": "Source depot reserves are too low to resupply.",
      "bottleneck_severed": "The transport route is disrupted or the station has only one route and it is unavailable.",
      "other": "Does not fit the other categories.",
    }),
  "auto_approve": Noul(
    instructions="Is `proposed_dispatch` safe to execute without human review?",
    criteria={
      "true": "Quantity is within `station_available_ullage` and `route_max_shipment`, the route is AVAILABLE, and no unusual edge case is present.",
      "false": "Any bound is tight or violated, data is stale, or the situation is unusual and needs a human.",
    }),
}
```
- State = structured JSON object: `station, fuel_type, current_inventory_liters, tank_capacity_liters, in_flight_liters, station_available_ullage, burn_rate_liters_per_hour, time_to_empty_ticks, stockout_risk, demand_multiplier, active_events[], route_status, depot_inventory_liters, proposed_dispatch{qty, route_id, depot_id, transit_ticks}, route_max_shipment, data_stale`.
- One `system_one` call per proposal (all three questions in one request). Run proposals concurrently with `asyncio.Semaphore(8)`; wrap each call with breaker `jev` and timeout `JEV_TIMEOUT_MS`. Disable or limit SDK auto-retries so the breaker sees failures.
- Read answers: `a = resp.answers`; `urgency_1to5 = a["urgency"].score + 1` (C6); `a["urgency"].confidence`, `.probabilities`; `a["crisis_class"].choice`, `.confidence`; `a["auto_approve"].noul` (no confidence, C8).
- Cache: if an identical (station, fuel, rounded state) was triaged within the last 4 ticks, reuse the result.
- Log each call to `ai_calls`; metrics `jev_latency_seconds`, `jev_calls_total{ok}`.
- If `crisis_class == bottleneck_severed` → trigger an immediate re-plan and an incident brief. If `upstream_starvation` → alert + next cycle raises depot-reserve weight for the other depot.

**Deterministic urgency (always computed, used when Jev is unavailable):** level from `T_empty`: >24 → 1, 16–24 → 2, 8–16 → 3, 4–8 → 4, <4 → 5 (and stock% rules for levels 1–2).

### 11.7 Gating policy (from the plans + brief guardrails)
```
if data_stale or simulator DEGRADED by stale fault:          → STAGED_REVIEW
elif jev breaker OPEN (cloud AI down):                        → AUTO_APPROVED, origin=HEURISTIC_FALLBACK
                                                                (deterministic checks passed; brief: dispatch continues)
elif forecast source == BASELINE_FALLBACK and risk low:       → AUTO_APPROVED, origin=HEURISTIC_FALLBACK
elif auto_approve ≥ AUTO_APPROVE_NOUL_MIN (0.85)
     and urgency_1to5 < AUTO_APPROVE_URGENCY_MAX (4.5)
     and urgency.confidence ≥ 0.4:                            → AUTO_APPROVED, origin=SYSTEM1_AUTO
else:                                                          → STAGED_REVIEW (low confidence or critical)
```
Staged decisions reserve their quantity (counted in `F` and `U`) until approved, rejected, or expired.
Note for the demo: at 8 ticks/s a human review lasts many ticks. `STAGED_DECISION_TTL_TICKS` expires stale proposals; approval always re-runs pre-flight on fresh state before POST. Recommend running the demo with `SIMULATION_SPEED=1` or `2` (config only; image unchanged).

### 11.8 Detection (`detect.py`) → `alerts`
- **Demand anomaly:** `(observed − forecast)/σ > 3` for 2 consecutive ticks.
- **Abnormal inventory:** station below safety stock; depot below reserve `R`.
- **Bottleneck:** route DISRUPTED or scheduled disruption within `L_max` ticks; single-route station affected → severity HIGH with "no alternate path".
- **Regional disruption:** ≥2 stations in a region with stockout risk ≥ 0.5.
- **Supply:** arrival DELAYED or quantity reduced (compare to first-seen quantity).
- **Outage:** station OUTAGE.
- **Stockout imminent:** `T_empty < L_min + 2`.

### 11.9 Executor with outbox (`executor.py`)
1. Before any POST, the decision row exists with `idempotency_key = "fsp-" + decision_id` and status `COMMITTING` (outbox).
2. Re-run pre-flight on fresh state. If it fails → `SIM_REJECTED` with our code, trigger re-plan (don't POST).
3. POST via sim client (same key/body on retries).
4. 200/201 → `COMMITTED`, store `sim_allocation_id`.
5. 409 `DISPATCH_CAPACITY_EXCEEDED` → back to APPROVED, retry next tick (same key, same body).
6. Other 409 → `SIM_REJECTED`, store code, alert, re-plan. `IDEMPOTENCY_KEY_MISMATCH` → critical bug alert (never change a body under an existing key).
7. 404 → config alert. 422 → bug alert. 503 exhausted → keep `COMMITTING`; the **reconciler** (every 5 s) retries with the same key; the simulator deduplicates.
8. On service start, reconcile all `COMMITTING` rows the same way.
9. Chunking: if `ROUTE_CAP_MODE=per_allocation` and qty > max_shipment, create child decisions each with its own key.
10. Metrics: `allocations_committed_total{origin}`, `allocation_rejections_total{code}`, `executor_post_latency_seconds`.

### 11.10 Cancellation of doomed allocations (`cancel.py`)
If a PENDING allocation's route is DISRUPTED or a route_disruption becomes ACTIVE/SCHEDULED at its departure tick → `POST /v1/allocations/{id}/cancel` (refunds depot, avoids a FAILED count). 409 `CANNOT_CANCEL` is fine (already departed). Audit every cancel.

### 11.11 What-if simulation (`whatif.py`)
`POST /whatif` `{proposals[] | {station_id,fuel_type,qty,route_id}, overrides?: {depot_derate, route_disabled[], demand_multiplier{}}}` → per affected station/fuel: projected inventory curve, T_empty, stockout_risk before/after, depot remaining, validator result. Pure computation, **never** POSTs. Used by the UI "Simulate" button, copilot tool, and load tests.

### 11.12 API
`POST /cycle/run` (dry_run flag), `POST /whatif`, `GET /decisions?status=`, `POST /decisions/{id}/approve {qty?, route_id?, operator, note}`, `POST /decisions/{id}/reject`, `POST /decisions/manual` (operator-created, origin OPERATOR_MANUAL), `GET /status` (breakers, last cycle, mode NORMAL/DEGRADED/FALLBACK), `/health`, `/metrics`.
Approve with edits → origin `SYSTEM2_OVERRIDE`, counted in `operator_overrides_total`, audited.

### Acceptance (integration tests using `/admin/reset`, `/admin/pause`, `/admin/step`)
- 500-tick run with no injected events: **zero HTTP 409 from the simulator** and `service_level ≥ 0.99` (record actual value).
- Tongi demand_spike 1.8× on region-dhaka: T_empty drops, proposals raised, no stockout if route available.
- route-gazipur-mirpur disrupted: Mirpur uses route-patiya-mirpur; Tongi shows "no alternate path" alert.
- Scheduled disruption on route-gazipur-tongi visible 8 ticks ahead → pre-stocking proposal before it starts.
- Kill forecast-svc: cycles continue with baseline, origin HEURISTIC_FALLBACK.
- Invalid TYPESAFE key: breaker opens after 5 failures, heuristic auto-dispatch continues.
- Retry test: simulate 503 after commit → retry returns existing allocation, no duplicate.

---

## 12. Phase 6 — Cognitive Service (LangChain + Gemini, System 2)

### 12.1 Model
```python
from langchain_google_genai import ChatGoogleGenerativeAI
llm = ChatGoogleGenerativeAI(model=settings.GEMINI_MODEL, temperature=0.1, api_key=settings.GEMINI_API_KEY,
                             timeout=settings.GEMINI_TIMEOUT_MS/1000, max_retries=1)
```
Wrap every call with breaker `gemini` (3000 ms). Log to `ai_calls`.

### 12.2 Decision explanation = computed facts + LLM narrative
**Facts are computed by code** (decision-svc `facts` JSON) and match the brief §4.3 fields:
`alert_entity_id, fuel_type, projected_stockout_hours (T_empty / ticks_per_hour), current_inventory_liters, expected_demand_liters (horizon sum), burn_rate_liters_per_hour, recommended_allocation_liters, source_depot_id, transit_route_id, stockout_risk_before, stockout_risk_after, binding_constraints[], confidence{forecast_sigma, jev_urgency_conf, jev_auto_approve}, alternatives[] (other feasible routes/depots with what-if results)`.

**Gemini only fills the narrative** via `llm.with_structured_output(DispatchNarrative)`:
```python
class DispatchNarrative(BaseModel):
    summary: str                      # 1-2 sentences for the operator
    primary_causal_factors: list[str] # root causes, grounded in the facts
    risk_mitigation_delta: str        # must quote stockout_risk_before/after from facts
    binding_constraints_explained: list[str]
    fallback_contingency: str         # must reference an entry of facts.alternatives or say none exists
    operator_checks: list[str]        # what the operator should verify
```
Prompt: system = "You are the System 2 diagnostic engine... Use ONLY numbers present in FACTS. Never invent quantities, routes, depots, or probabilities. Analyze root causes." Human = FACTS JSON + active events + Jev triage + recent alerts.
**Post-validation:** every number appearing in the narrative must exist in FACTS (regex check); any route/depot id must exist. If validation fails or Gemini is unavailable → **template explanation** generated from facts (`source=TEMPLATE`). The UI always renders the facts table; the narrative is additive.

### 12.3 Incident briefs
When a sim event becomes ACTIVE (or a compound crisis: ≥2 active events), build `IncidentBrief{title, affected_entities[], impact_summary, expected_duration_ticks, recommended_operator_actions[], lifelines_at_risk[]}` from facts; store in `incidents`. Template fallback on failure.

### 12.4 Operator copilot (tool-calling agent)
- Tools (**read-only**, call gateway/decision/forecast internal APIs, never the simulator write endpoint):
  `get_network_state()`, `get_station(station_id)`, `get_depot(depot_id)`, `get_routes()`, `get_events(status?)`, `get_supply_arrivals()`, `get_forecast(station_id, fuel_type)`, `get_decisions(status?)`, `simulate_allocation(station_id, fuel_type, qty, route_id, overrides?)` → real `/whatif` (C12), `propose_allocation(...)` → creates a **STAGED_REVIEW** decision only (operator must approve in the UI).
- Build with LangChain tool calling (`llm.bind_tools(tools)` loop, or the agent constructor available in the installed LangChain version — verify). Max 6 tool steps; per-turn timeout 20 s.
- Grounding rule in the system prompt: answer only from tool outputs; if data is missing, say so. Include simulation-only disclaimer.
- Example the copilot must handle: "If Gazipur depot throughput is degraded by 50% for the next 12 ticks, can Patiya sustain Mirpur's diesel without causing a stockout at Karnaphuli?" → uses `simulate_allocation` with overrides.
- Streaming responses to the UI via gateway WebSocket (or SSE).

### 12.5 API
`POST /explain {decision_id}`, `POST /incident {sim_event_id}`, `POST /copilot/chat {session_id, message}`, `/health`, `/metrics` (`gemini_latency_seconds`, `gemini_calls_total{ok,purpose}`, `explanations_total{source}`).

### Acceptance
- 100% of generated narratives pass schema + number-grounding validation or fall back to template.
- With `GEMINI_API_KEY` invalid, explanations still appear (template) and nothing else breaks.
- Copilot answers the example question using ≥ 2 tool calls with real numbers.

---

## 13. Phase 7 — Gateway + Frontend

### 13.1 Gateway (FastAPI BFF)
- `GET /api/overview` (tick, sim_time, status, KPIs from `/v1/metrics`, mode, alerts count)
- `GET /api/stations|depots|routes|supply|events|allocations|alerts|incidents|audit`
- `GET /api/forecasts?station_id&fuel_type`
- `GET /api/decisions?status`, `POST /api/decisions/{id}/approve|reject`, `POST /api/decisions/manual`, `POST /api/whatif`
- `POST /api/copilot/chat` (streams)
- `GET /api/health/components` → aggregated health (§15.3)
- `WS /ws` → pushes `state.updated`, `decisions.updated`, `alerts`, `sim.fault`, `health` (fan-out from Redis)
- Demo controls (only when `DEMO_CONTROLS=true`): `POST /api/demo/{run|pause|step|reset}`, `POST /api/demo/events`, `POST /api/demo/faults`, `POST /api/demo/faults/clear` → proxy to simulator `/admin/*`; audited.
- Operator identity: simple header/name field is enough; record in audit.

### 13.2 Frontend pages (React + TS + Vite + TanStack Query + Recharts + React-Leaflet)
Global: header with tick, sim_time, RUNNING/PAUSED, mode badge (NORMAL / DEGRADED / FALLBACK), **"SIMULATED DATA"** label, stale-data banner, alert bell.
1. **Overview / Network map** — depots and stations with approximate display coordinates (Gazipur, Mirpur, Tongi, Patiya, Karnaphuli, Cox's Bazar — config file, labeled approximate), routes colored by status, KPIs (service level, unmet liters, allocation failures, in-flight).
2. **Inventory** — tank gauges per depot/station/fuel (inventory, capacity, in-flight, safety stock line).
3. **Demand & Risk** — observed vs forecast charts with σ band; stockout-risk heatmap (station × fuel); T_empty table.
4. **Supply & Disruptions** — supply-arrival timeline (SCHEDULED/DELAYED/ARRIVED), events feed (scheduled/active/resolved), route status, incident briefs.
5. **Decision Center** — staged queue; each card shows the facts table (§12.2), Jev scores (urgency, crisis class, auto-approve), narrative, alternatives, **Simulate** (what-if), **Approve / Edit & Approve / Reject**; history of all decisions with origin and outcome.
6. **Copilot** — chat with visible tool calls.
7. **System Health** — component status (Healthy/Degraded/Down), breaker states, p95 latency and error rate, SSE status, fallback activations; link to Grafana.
8. **Audit** — our audit log + allocation ledger.

### Acceptance
- An operator can go from alert → decision → simulate → approve → see allocation IN_TRANSIT → ARRIVED without leaving the UI.
- UI stays usable (last-good data + banners) during every fault type.

---

## 14. Phase 8 — Resilience & Chaos

### 14.1 Failure matrix (implement, test, document in `docs/RESILIENCE_REPORT.md`)
| Failure | Signal | Handling | Expected outcome |
|---|---|---|---|
| Transient 503 (`unavailable`, `error_rate`) | `{"error":{"code":"FAULT_INJECTED"}}` | Backoff+jitter retries; POST keeps same idempotency_key; reconciler | No duplicate allocations; state reconciles after fault |
| Latency fault | slow responses | Timeouts > delay; single-flight loop skips ticks | No pile-up, UI shows degraded latency |
| Stale data | `X-Simulator-Stale: true` | Flag snapshots, auto-commit suspended, banner | No autonomous dispatch on stale data |
| SSE drop / `stream_disconnect` | 503 `{"detail":...}` / EOF | Reconnect with jitter + full REST sync; polling fallback | Ticks keep flowing |
| SSE backlog >200 | silent drop | Non-blocking reader; reconnect on silence > 45 s | Recovered via full sync |
| Malformed payload | Pydantic error | Reject, keep last valid cache, alert | No crash |
| forecast-svc down | breaker `forecast` | Baseline forecast + (s,S) heuristic | Dispatches continue |
| Jev down / 401 / 429 / 529 | breaker `jev` | Deterministic urgency, heuristic gating | Dispatches continue |
| Gemini down | breaker `gemini` | Template explanations, copilot says unavailable | Decision Center still usable |
| Postgres restart | connection errors | Pool pre-ping, retries, services report DEGRADED | Recover automatically |
| Redis down | bus errors | decision-svc falls back to polling `sim_ticks` every 1 s | Loop continues |
| decision-svc restart | — | Outbox reconciler resumes COMMITTING with same keys | No lost or duplicate dispatch |
| Route disrupted | event ACTIVE | Exclude route, reroute (Mirpur/Karnaphuli), cancel PENDING on it, "no alternate path" alert (Tongi/Cox's Bazar) | Fewer FAILED allocations |
| Compound crisis (spike + delay) | multiple events | Depot reserve weighting, cross-region routes, incident brief | Critical stations protected |

### 14.2 Chaos scripts (`scripts/chaos/`)
Shell/Python scripts that call simulator `/admin/faults` and `/admin/events`, `docker compose stop forecast-svc`, break the Jev/Gemini key (restart with `CHAOS_BAD_AI_KEYS=true` which makes clients use an invalid key), and restart services. Each script prints what to observe in UI/Grafana.

### Acceptance
Every row above has an automated or scripted test with recorded evidence (screenshots/log excerpts/metrics) in `RESILIENCE_REPORT.md`.

---

## 15. Phase 9 — Observability

### 15.1 Metrics (Prometheus, every service exposes `/metrics`)
- **Application:** `http_requests_total`, `http_request_duration_seconds` (p50/p95/p99 via histogram_quantile), error rate, uptime.
- **Infrastructure:** cAdvisor CPU, memory, I/O per container.
- **Simulator integration:** `sim_tick`, `sim_service_level`, `sim_unmet_liters`, `sim_allocation_failures`, `sim_client_latency_seconds`, `sim_faults_detected_total{type}`, `sse_connected`, `sse_reconnects_total`, `sync_lag_ticks`.
- **Model/intelligence:** `forecast_mae{station,fuel}`, `forecast_source_total`, `stockout_risk{station,fuel}`, `decision_cycle_seconds`, `decisions_total{origin,status}`, `allocation_rejections_total{code}`, `fallback_activations_total{dependency}`, `breaker_state{dependency}`, `jev_latency_seconds`, `gemini_latency_seconds`, `alerts_total{kind}`, `operator_overrides_total`.

### 15.2 Grafana (provisioned automatically)
Dashboards: *Platform Overview*, *Simulator Integration*, *Intelligence & Decisions*, *Infrastructure*, *Load Test*. Alert rules: service down, p95 > 250 ms, breaker OPEN, stale data, service_level < 0.95.

### 15.3 Health console (`GET /api/health/components`)
Components and status (Healthy / Degraded / Down): Backend API (gateway), Database, Simulator connection (health + last successful `/v1` call + stale), SSE stream, Ingestion, Forecast (inference service), Decision optimization engine, Jev (breaker), Gemini (breaker), Redis. Plus live p95 latency (ms) and system error rate (%) from Prometheus queries.

### 15.4 Logs & audit
Structured JSON logs everywhere with `tick`, `decision_id`, `idempotency_key`. `audit_log` records: allocations executed, rejections, cancels, integration failures, fallback activations, operator approvals/overrides/rejections, demo control actions.

### Acceptance
Grafana shows live data from all services after `docker compose up`; the health console flips correctly when a component is stopped.

---

## 16. Phase 10 — Load Testing (Locust)

- Target critical paths **without mutating the simulator**: `POST /whatif` and `POST /cycle/run?dry_run=true` (decision API), `POST /forecast` (prediction API), `GET /api/overview` and `GET /api/decisions` (gateway), heuristic engine endpoint.
- Profile: ramp to 50 concurrent users over 60 s, hold 5 min, with the simulator RUNNING in the background.
- Report in `docs/LOADTEST_REPORT.md` (mandatory per brief): average latency, p50, p95, p99, throughput (RPS), error rate %, concurrency, peak CPU/RAM per container (from cAdvisor/`docker stats`), and the saturation point (increase users until p95 or errors degrade).
- Targets from the plan (goals, not hard requirements): ingestion/state p95 < 42 ms at >150 RPS; forecast/LP solver p95 < 65 ms at >80 RPS; heuristic p99 < 5 ms; overall p95 < 250 ms; 0% unhandled 5xx. Report actuals honestly.

---

## 17. Phase 11 — Tests, CI/CD, Documentation

### 17.1 Tests
- Unit: sim client, SSE parser, breaker, validator (each 409 branch), MILP never violates constraints (property test with random states), heuristic, gating policy, number-grounding checker, feature pipeline (no leakage).
- Integration (`tests/integration/test_simulator_pipeline.py`) against the real image: reset → pause → POST allocation → step until ARRIVED → assert inventory moved; idempotent replay returns same id; mismatched body → 409; cancel PENDING refunds; full platform cycle in dry-run and live mode.
- Deterministic scenario tests using `/admin/step` for reproducibility.

### 17.2 CI (`.github/workflows/ci.yml`)
Source → Build → Test → Package → Deploy → Health check:
1. `ruff` + `mypy` (shared + services) + frontend `eslint`/`tsc`.
2. Unit tests.
3. `docker compose build`.
4. `docker compose up -d` (with dummy AI keys → exercises fallbacks), wait for health, run integration tests, `docker compose down`.
5. Secret scan (gitleaks) — fail on findings.
6. On `main`: push images (optional) and tag.

### 17.3 Documentation
- `README.md`: overview, architecture image, prerequisites, `cp .env.example .env`, `docker compose up`, URLs (UI, gateway docs, Grafana, Prometheus, simulator admin), how to run tests/chaos/load, known limitations.
- `docs/ARCHITECTURE.md`: Mermaid diagrams for deployment, data flow, decision cycle sequence, degradation paths; export `architecture.png`.
- `docs/DECISIONS.md`, `docs/API.md`, `MODEL_REPORT.md`, `RESILIENCE_REPORT.md`, `LOADTEST_REPORT.md`, `DEMO_RUNBOOK.md`.

---

## 18. Phase 12 — Demo Runbook (brief's 14-step storyline)

Write `docs/DEMO_RUNBOOK.md` and `scripts/demo/` so each step is one command or one UI click. Recommended: `SIMULATION_SPEED=2` for the demo; `make demo-reset` = `/admin/reset` + clear faults + restart decision-svc state.

| # | Step | How |
|---|---|---|
| 1 | Normal operations | `/admin/run`; Overview shows steady KPIs, all green |
| 2 | Operator dashboard | Walk map, tank gauges, supply timeline, health console |
| 3 | Demand increase | `POST /admin/events {"type":"demand_spike","start_tick":<now+2>,"duration_ticks":24,"parameters":{"station_ids":["station-tongi"],"multiplier":1.8}}` |
| 4 | Risk detection | Demand anomaly + rising stockout-risk alerts appear |
| 5 | Prediction | Demand & Risk page: forecast adapts, T_empty and risk shown |
| 6 | Recommendation | MILP proposal (e.g. diesel on route-gazipur-tongi, capped by 6,500 L max_shipment), Jev urgency/crisis class; staged if urgency ≥ 4.5 or low confidence |
| 7 | Operator inspection | Decision card: facts table, binding constraints, Gemini narrative, alternatives |
| 8 | Simulate & execute | Click Simulate (what-if) → Approve → 201 → allocation PENDING → IN_TRANSIT → ARRIVED |
| 9 | Crisis injected | `route_disruption` on `route-gazipur-mirpur` (reroutable) **and/or** `depot_constraint` on depot-gazipur; optionally `shipment_delay` for a compound crisis |
| 10 | Dynamic adaptation | Mirpur served via `route-patiya-mirpur`; PENDING on disrupted route cancelled; Tongi (if its route is hit) shows "no alternate path" + pre-stock/priority plan; incident brief generated |
| 11 | Dependency failure | `docker compose stop forecast-svc` and/or run with invalid AI keys |
| 12 | Monitoring alarm | Health console + Grafana: component Down, breaker OPEN, fallback counter rising |
| 13 | Fallback activation | Mode badge FALLBACK; (s,S) heuristic dispatches with origin HEURISTIC_FALLBACK |
| 14 | Operations continue | Service level holds; restart service → breaker closes → mode NORMAL. Also show a simulator fault (`error_rate`, `stale_data`) handled live |

---

## 19. Definition of Done (map to official rubric)

| Rubric (weight) | Must be true |
|---|---|
| Working Product & UX (20%) | End-to-end operator workflow in UI; all 8 pages functional; clear states/banners |
| Intelligence & Decisions (20%) | Ridge forecasts beat baselines (report); MILP + validator give zero 409s in 500-tick run; Jev triage + gating; Gemini grounded explanations; detection alerts |
| Architecture & Integration (15%) | Clean service boundaries; full `/v1/*` usage incl. SSE; idempotent writes; cancellation; follows validation order |
| DevOps & Engineering Quality (15%) | `docker compose up` works from clean clone; CI green; tests; docs; no secrets in repo |
| Resilience & Incident Response (10%) | Every row of §14.1 demonstrated with evidence |
| Observability & Load Test (10%) | Grafana dashboards, health console, structured logs, audit; load-test report with all mandatory metrics |
| Demo & Problem Understanding (10%) | Runbook rehearsed; each of the 14 steps scripted; team can explain corrections in §1 (e.g., single-route stations) |

### Final checklist before submission
- [ ] Fresh clone → `cp .env.example .env` → fill keys → `docker compose up` → everything healthy.
- [ ] Platform works with **empty AI keys** (fallback mode) — nothing crashes.
- [ ] 500-tick baseline run recorded: service level, unmet liters, allocation failures, 409 count = 0.
- [ ] All reports in `docs/` filled with real numbers (no placeholders).
- [ ] `git grep` for secrets clean; `.env` not committed.
- [ ] Simulator image unmodified and pinned to `1.0.0`.
