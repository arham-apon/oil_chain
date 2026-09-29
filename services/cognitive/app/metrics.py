from __future__ import annotations

from prometheus_client import Counter, Histogram

GEMINI_LATENCY = Histogram("gemini_latency_seconds", "Gemini call latency", buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 20))
GEMINI_CALLS = Counter("gemini_calls_total", "Gemini calls", ["ok", "purpose"])
EXPLANATIONS = Counter("explanations_total", "Explanations produced", ["source"])
INCIDENTS = Counter("incident_briefs_total", "Incident briefs produced", ["source"])
COPILOT_TURNS = Counter("copilot_turns_total", "Copilot turns", ["source"])
