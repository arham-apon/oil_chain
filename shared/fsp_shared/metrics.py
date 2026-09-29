"""Common Prometheus metrics, the FastAPI middleware and the ``/metrics`` endpoint."""

from __future__ import annotations

import time

from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --- HTTP (every service) ----------------------------------------------------------------
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
SERVICE_START_TIME = Gauge("service_start_time_seconds", "Unix time the service started", ["service"])

# --- simulator client ----------------------------------------------------------------------
SIM_CLIENT_REQUESTS = Counter(
    "sim_client_requests_total", "Simulator client requests", ["endpoint", "method", "status"]
)
SIM_CLIENT_LATENCY = Histogram(
    "sim_client_latency_seconds",
    "Simulator client request latency",
    ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5),
)
SIM_FAULTS_DETECTED = Counter(
    "sim_faults_detected_total", "Simulator faults observed by the client", ["type"]
)

# --- circuit breakers ------------------------------------------------------------------------
BREAKER_STATE = Gauge(
    "breaker_state", "Circuit breaker state (0 closed, 1 half-open, 2 open)", ["dependency"]
)
FALLBACK_ACTIVATIONS = Counter(
    "fallback_activations_total", "Times a fallback path replaced a dependency", ["dependency", "reason"]
)


class PrometheusMiddleware:
    """Pure-ASGI middleware: request counter + latency histogram labelled with the route template."""

    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            HTTP_REQUESTS.labels(self.service, path, method, str(status["code"])).inc()
            HTTP_LATENCY.labels(self.service, path).observe(time.perf_counter() - start)


async def _metrics_endpoint(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_metrics(app: FastAPI, service: str) -> None:
    """Add the middleware and ``GET /metrics`` to a FastAPI app."""
    SERVICE_START_TIME.labels(service).set_to_current_time()
    app.add_middleware(PrometheusMiddleware, service=service)
    app.add_route("/metrics", _metrics_endpoint, include_in_schema=False)
