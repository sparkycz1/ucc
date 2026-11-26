"""How many release assessments may run at once, across every worker.

"Assess all", a catalogue refresh or a first sync can enqueue dozens of
assessments in one go; Celery would start as many as it has worker
processes, all hitting the model at the same moment — a local model
(Ollama, vLLM, a LiteLLM gateway with its own short timeout) then times out
on most of them. So an assessment takes one of
`AppSettings.ai_max_concurrent_assessments` slots before it starts, and
when none is free it re-enqueues itself a little later instead of waiting
(a waiting task would hold a worker process the inventory syncs need).

The slots are a Redis sorted set shared by every worker: member = a
random token, score = when it expires. A slot always expires after
`ttl` seconds (the task's own time limit), so a worker killed mid-task
can't leak one for good. Acquiring drops expired members and adds one
only below the limit, atomically, in one Lua script.

Best-effort like `app.services.live_updates`: when Redis can't be reached
the assessment runs anyway (`UNLIMITED`) — throttling is a courtesy to the
model, not something worth failing an assessment over.
"""

from __future__ import annotations

import logging
import secrets
import time

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SLOTS_KEY = "ucc:ai:assessment-slots"
# Handed out when Redis is unreachable; `release` ignores it.
UNLIMITED = "unlimited"

_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


def _client() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url)


async def acquire(limit: int, ttl: int) -> str | None:
    """A slot's token, or `None` when all `limit` are taken."""
    token = secrets.token_hex(8)
    now = time.time()
    try:
        client = _client()
        try:
            got = await client.eval(
                _ACQUIRE, 1, SLOTS_KEY, now, now + ttl, max(limit, 1), token, ttl
            )
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - best-effort, see the module docstring
        logger.warning("ai slots: Redis unreachable, assessing without a limit")
        return UNLIMITED
    return token if int(got) == 1 else None


async def release(token: str) -> None:
    if token == UNLIMITED:
        return
    try:
        client = _client()
        try:
            await client.zrem(SLOTS_KEY, token)
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - it expires on its own anyway
        logger.warning("ai slots: could not release a slot; it expires on its own")
