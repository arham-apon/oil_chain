"""Redis Streams helpers: ``XADD fsp:events`` with ``MAXLEN ~ 10000`` and per-service consumer groups."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from .schemas import BusMessage

STREAM_KEY = "fsp:events"
MAXLEN = 10_000


class Bus:
    def __init__(self, redis: aioredis.Redis, stream: str = STREAM_KEY) -> None:
        self.redis = redis
        self.stream = stream

    @classmethod
    def from_url(cls, url: str) -> Bus:
        return cls(aioredis.from_url(url, decode_responses=True))

    async def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> str:
        fields = {
            "type": event_type,
            "payload": json.dumps(payload or {}, default=str),
            "ts": repr(time.time()),
        }
        message_id = await self.redis.xadd(self.stream, cast(Any, fields), maxlen=MAXLEN, approximate=True)
        return str(message_id)

    async def ensure_group(self, group: str, start: str = "$") -> None:
        """Create the consumer group if missing. ``start='$'`` = only new events, ``'0'`` = the whole stream."""
        try:
            await self.redis.xgroup_create(self.stream, group, id=start, mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self, group: str, consumer: str, *, count: int = 50, block_ms: int = 1000
    ) -> list[BusMessage]:
        """Read new messages for this consumer. Returned messages must be :meth:`ack`-ed."""
        resp: Any = await self.redis.xreadgroup(
            group, consumer, {self.stream: ">"}, count=count, block=block_ms
        )
        return [self._decode(mid, fields) for _, entries in resp or [] for mid, fields in entries]

    async def ack(self, group: str, *ids: str) -> None:
        if ids:
            await self.redis.xack(self.stream, group, *ids)

    async def listen(self, group: str, consumer: str, *, block_ms: int = 1000) -> AsyncIterator[BusMessage]:
        """Endless auto-acking iterator over messages (creates the group at ``$`` if needed)."""
        await self.ensure_group(group)
        while True:
            for msg in await self.read(group, consumer, block_ms=block_ms):
                yield msg
                await self.ack(group, msg.id)

    @staticmethod
    def _decode(message_id: str, fields: dict[str, str]) -> BusMessage:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except ValueError:
            payload = {"_raw": fields.get("payload")}
        return BusMessage(
            id=message_id, type=fields.get("type", ""), payload=payload, ts=float(fields.get("ts", 0) or 0)
        )

    async def close(self) -> None:
        await self.redis.aclose()
