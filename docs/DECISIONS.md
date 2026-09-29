# Decisions & Deviations

| # | Decision | Reason |
|---|----------|--------|
| D1 | Repo root is `oil_chain/` (the spec's `fuel-ops-platform/`). | Existing git repo. |
| D2 | Local dev venv uses Python 3.13 (only version installed); code targets 3.12 (Docker base `python:3.12-slim`) and avoids 3.13-only features. | No 3.12 on the dev machine. |
| D3 | Simulator returns `sim_time` **without timezone** (`2026-01-01T00:00:00`), though the guide shows `+00:00`. `timeutil.parse_sim_time` treats naive values as UTC. | Observed against the live image. |
| D4 | Retry backoff is hand-written (full jitter) rather than `tenacity`, so POST retries can guarantee an identical body/key and metrics hook in cleanly. | Spec §7.2 |
| D5 | Simulator 200 and 201 on POST /v1/allocations are both treated as success (C4). | Spec C4 |
