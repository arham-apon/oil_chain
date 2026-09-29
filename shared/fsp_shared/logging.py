"""structlog JSON logging with the platform's standard fields.

Every record carries ``service``, ``level`` and ``event``; ``tick``, ``decision_id``,
``idempotency_key`` and ``trace_id`` appear whenever they are bound with :func:`bind_context`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

CONTEXT_FIELDS = ("tick", "decision_id", "idempotency_key", "trace_id")


def configure_logging(service: str, level: str = "INFO", stream: Any = None) -> None:
    def add_service(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict["service"] = service
        return event_dict

    out = stream or sys.stdout
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_service,
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, httpx, ...) through the same JSON pipeline.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn installs its own plain-text handlers: hand its loggers to the JSON root handler instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # one INFO line per outgoing HTTP request drowns everything else (health probe runs every 2 s)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def bind_context(**fields: Any) -> None:
    """Bind fields (``tick``, ``decision_id``, ...) to every subsequent log line in this task/context."""
    structlog.contextvars.bind_contextvars(**fields)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
