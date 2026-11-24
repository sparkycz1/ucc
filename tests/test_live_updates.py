"""app/services/live_updates.py (the Redis pub/sub "something changed, go
check" doorbell) and the jobs in app/tasks/jobs.py that ring it.

No real Redis is touched: `aioredis.from_url` is monkeypatched to a fake
client that just records what would have been published — same "no real
infrastructure in tests" rule this suite already follows for Celery/Redis
elsewhere (see tests/conftest.py's module comment). The WebSocket relay
itself (app/web/routes/live_ws.py) has no dedicated test here: this
project's async test client (httpx + ASGITransport) has no WebSocket
support, so that module is exercised for real only by running the app.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.tasks.jobs as jobs
from app.core.security import encrypt_secret
from app.db.models.fabric import Fabric
from app.services import live_updates
from app.unifi import sync as unifi_sync
from app.unifi.client import SiteManagerError


class _FakePublishedRedis:
    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink

    async def publish(self, channel: str, message: str) -> None:
        self._sink.append((channel, message))

    async def aclose(self) -> None:
        return None


class _FakeAioredisModule:
    """Stands in for the whole `redis.asyncio` module (imported in
    live_updates.py as `aioredis`) — swapping the binding wholesale rather
    than patching `.from_url` on the real, shared module object."""

    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink

    def from_url(self, *args: object, **kwargs: object) -> _FakePublishedRedis:
        return _FakePublishedRedis(self._sink)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every (channel, message) `publish_fabric_event` would have sent."""
    sink: list[tuple[str, str]] = []
    monkeypatch.setattr(live_updates, "aioredis", _FakeAioredisModule(sink))
    return sink


def test_channel_for_is_namespaced_per_fabric():
    assert live_updates.channel_for("abc") == "ucc:live:fabric:abc"
    assert live_updates.channel_for("abc") != live_updates.channel_for("def")


async def test_publish_fabric_event_sends_the_kind_as_json(published):
    await live_updates.publish_fabric_event("f1", live_updates.KIND_INVENTORY)

    assert len(published) == 1
    channel, message = published[0]
    assert channel == "ucc:live:fabric:f1"
    assert json.loads(message) == {"kind": "inventory"}


class _BoomingAioredisModule:
    def from_url(self, *args: object, **kwargs: object) -> _FakePublishedRedis:
        raise ConnectionError("no redis here")


async def test_publish_fabric_event_is_best_effort_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Redis connection must never raise out of this — see the
    module docstring's reasoning: a missed push just means the polling
    fallback catches it a little later."""
    monkeypatch.setattr(live_updates, "aioredis", _BoomingAioredisModule())

    await live_updates.publish_fabric_event("f1", live_updates.KIND_UPDATES)  # must not raise


async def _make_fabric(db_session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with db_session_factory() as session:
        fabric = Fabric(name="live-updates-target", api_key_encrypted=encrypt_secret("k"))
        session.add(fabric)
        await session.commit()
        return fabric.id


@pytest.fixture
def jobs_db(db_session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "AsyncSessionLocal", db_session_factory)
    return db_session_factory


async def test_successful_sync_publishes_an_inventory_event(
    jobs_db: Any, published: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fabric_id = await _make_fabric(jobs_db)

    async def _fake_fetch(client: object) -> unifi_sync.Inventory:
        return unifi_sync.Inventory(hosts=[], sites=[], devices=[])

    monkeypatch.setattr(unifi_sync, "fetch_inventory", _fake_fetch)
    result = await jobs._sync_fabric(str(fabric_id))

    assert result["ok"] is True
    assert published == [(f"ucc:live:fabric:{fabric_id}", json.dumps({"kind": "inventory"}))]


async def test_failed_sync_is_recorded_on_the_fabric_and_audited_once(
    jobs_db: Any, published: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from app.db.models.audit_log import AuditLogEntry

    fabric_id = await _make_fabric(jobs_db)

    async def _failing_fetch(client: object) -> unifi_sync.Inventory:
        raise SiteManagerError("GET /v1/hosts: HTTP 401", status=401)

    monkeypatch.setattr(unifi_sync, "fetch_inventory", _failing_fetch)
    first = await jobs._sync_fabric(str(fabric_id))
    second = await jobs._sync_fabric(str(fabric_id))

    assert first["ok"] is False and second["ok"] is False
    assert published == []
    async with jobs_db() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None and "401" in (fabric.last_sync_error or "")
        entries = (
            (await db.execute(select(AuditLogEntry).where(AuditLogEntry.action == "fabric.sync")))
            .scalars()
            .all()
        )
    # Only the healthy -> failing transition is audited, not every sweep.
    assert len(entries) == 1
