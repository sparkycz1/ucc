"""What background work is running right now, for the activity indicator
in the page header (`app.web.routes.activity`).

A Redis sorted set per kind ("sync", "catalog"): member = what (a Fabric
id, "catalog"), score = when the mark expires, so a worker killed mid-job
can't leave something "running" for good. Jobs mark themselves with
`running(kind, key, ttl)` around their work. Assessments aren't here: the
assessment slots (`app.ai.slots`) already say how many run.

Best-effort like `app.services.live_updates`: without Redis nothing is
shown as running, and no job ever fails over it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "ucc:activity:"


def _client() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url)


async def _mark(kind: str, key: str, ttl: int | None) -> None:
    try:
        client = _client()
        try:
            if ttl is None:
                await client.zrem(_PREFIX + kind, key)
            else:
                await client.zadd(_PREFIX + kind, {key: time.time() + ttl})
                await client.expire(_PREFIX + kind, ttl)
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - informational only
        logger.debug("activity: Redis unreachable, %s %s not marked", kind, key)


async def start(kind: str, key: str, ttl: int) -> None:
    """Mark `key` as running from the moment a person asks for it (the
    route that enqueues the job), so the indicator shows it at once even
    before a worker picks it up; the job's own `running` clears it."""
    await _mark(kind, key, ttl)


@asynccontextmanager
async def running(kind: str, key: str, ttl: int) -> AsyncIterator[None]:
    """Mark `key` of `kind` as running for the duration of the block."""
    await _mark(kind, key, ttl)
    try:
        yield
    finally:
        await _mark(kind, key, None)


async def count(kind: str) -> int:
    """How many of `kind` are running now (expired marks don't count)."""
    try:
        client = _client()
        try:
            return int(await client.zcount(_PREFIX + kind, time.time(), "+inf"))
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - informational only
        return 0
