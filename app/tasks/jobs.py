"""Background jobs processed by Celery (broker + result backend in Redis).

Every job in here follows the same two-part shape:

- `async def _do_the_thing(...)` — the real work. This app's logic is async
  all the way down (SQLAlchemy's async sessions, httpx), so that is where
  it lives.
- `@celery_app.task(name="...") def do_the_thing(...)` — a thin synchronous
  Celery task that does nothing but `asyncio.run(...)` the coroutine above.
  Celery tasks are synchronous; this is the seam between the two worlds, and
  it is deliberately kept to one line so there is never any logic that only
  exists on the sync side.

Task names are given explicitly and are a stable contract — see
`app.tasks.celery_app`'s module docstring.

DB sessions are always opened as `db_session.AsyncSessionLocal(...)` through
the module, never via a `from app.db.session import AsyncSessionLocal`
binding: each forked Celery worker child rebuilds that factory after the fork
(again, see `app.tasks.celery_app`), and a name captured at import time would
keep pointing at the parent's connection pool.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from app.audit import log_event
from app.core.app_settings import get_or_create_app_settings
from app.db import session as db_session
from app.db.models.audit_log import AuditLogEntry, AuditOutcome
from app.db.models.fabric import Fabric
from app.db.models.notification_log import NotificationLog
from app.db.models.notification_rule import NotificationEventType
from app.services.live_updates import KIND_INVENTORY, publish_fabric_event
from app.services.notifications import notify
from app.tasks.celery_app import celery_app
from app.unifi.client import SiteManagerError
from app.unifi.sync import sync_fabric as apply_fabric_sync

logger = logging.getLogger(__name__)

_SYNC_ACTOR = "inventory sync (automatic)"


async def _sync_fabric(fabric_id: str) -> dict[str, Any]:
    """Mirror one Fabric's consoles/apps/sites/devices from Site Manager.

    Not audit-logged on success — it is an unattended periodic sweep (see
    CLAUDE.md's audit-log rule). A *failure* is recorded on the Fabric
    itself (`last_sync_error`) and audit-logged only on the transition from
    healthy to failing, so a key that has been revoked doesn't write one
    audit entry per sweep forever."""
    async with db_session.AsyncSessionLocal() as session:
        fabric = await session.get(Fabric, uuid.UUID(fabric_id))
        if fabric is None:
            return {"ok": False, "error": "Fabric not found."}
        try:
            result = await apply_fabric_sync(session, fabric)
        except SiteManagerError as exc:
            await session.rollback()
            fabric = await session.get(Fabric, uuid.UUID(fabric_id))
            if fabric is None:
                return {"ok": False, "error": "Fabric not found."}
            was_failing = fabric.last_sync_error is not None
            fabric.last_sync_error = str(exc)[:2000]
            await session.commit()
            if not was_failing:
                await notify(
                    session,
                    NotificationEventType.FABRIC_SYNC_FAILED,
                    fabric=fabric,
                    context={"details": str(exc)},
                )
                await log_event(
                    session,
                    actor=_SYNC_ACTOR,
                    action="fabric.sync",
                    summary=f'Inventory sync of Fabric "{fabric.name}" failed: {exc}',
                    outcome=AuditOutcome.FAILURE,
                    target_type="fabric",
                    target_id=fabric.id,
                    target_label=fabric.name,
                )
            logger.warning("sync_fabric(%s) failed: %s", fabric_id, exc)
            return {"ok": False, "error": str(exc)}
        await session.commit()
    await publish_fabric_event(fabric_id, KIND_INVENTORY)
    # Lazy: release_jobs imports this module's siblings, and is only needed
    # once a sync has succeeded.
    from app.tasks.release_jobs import discover_releases

    discover_releases.delay(fabric_id)
    return {
        "ok": True,
        "consoles": result.consoles,
        "sites": result.sites,
        "devices": result.devices,
    }


@celery_app.task(name="app.tasks.jobs.sync_fabric")
def sync_fabric(fabric_id: str) -> dict[str, Any]:
    return asyncio.run(_sync_fabric(fabric_id))


async def _sync_all_fabrics() -> None:
    """Beat sweep: enqueue one `sync_fabric` per enabled Fabric — never
    awaited inline, so one slow API key can't hold up the others."""
    async with db_session.AsyncSessionLocal() as session:
        result = await session.execute(select(Fabric.id).where(Fabric.is_enabled.is_(True)))
        fabric_ids = [str(fid) for fid in result.scalars().all()]
    for fabric_id in fabric_ids:
        sync_fabric.delay(fabric_id)


@celery_app.task(name="app.tasks.jobs.sync_all_fabrics")
def sync_all_fabrics() -> None:
    asyncio.run(_sync_all_fabrics())


_AUDIT_PURGE_ACTOR = "retention policy (automatic)"


async def _purge_old_audit_log_entries() -> None:
    """Delete audit log entries older than `AppSettings.
    audit_log_retention_days` — a fixed daily sweep, same shape as
    the other Beat-driven sweeps, since "once a day" needs no configurable
    interval of its own (only *how many days to keep* is configurable, on the
    Settings page).

    Only ever deletes from the oldest end (`created_at < cutoff`), never
    from the middle — the hash chain's tip (`AuditChainState.last_hash`)
    always reflects the newest entry regardless of what's pruned from the
    beginning, so this can never invalidate `app.audit.verify_chain` for
    the entries that remain (see the Architecture wiki page). Skipped
    entirely when retention is unset (`None` = keep forever, the default).
    """
    async with db_session.AsyncSessionLocal() as session:
        app_settings = await get_or_create_app_settings(session)
        retention_days = app_settings.audit_log_retention_days
        if not retention_days:
            return

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        count_result = await session.execute(
            select(func.count()).select_from(AuditLogEntry).where(AuditLogEntry.created_at < cutoff)
        )
        deleted_count = count_result.scalar_one()
        if not deleted_count:
            return

        await session.execute(delete(AuditLogEntry).where(AuditLogEntry.created_at < cutoff))
        await session.commit()

        await log_event(
            session,
            actor=_AUDIT_PURGE_ACTOR,
            action="audit_log.purge",
            summary=(
                f"Purged {deleted_count} audit log entr"
                f"{'y' if deleted_count == 1 else 'ies'} older than "
                f"{retention_days} day(s)"
            ),
            details={"deleted_count": deleted_count, "retention_days": retention_days},
        )


@celery_app.task(name="app.tasks.jobs.purge_old_audit_log_entries")
def purge_old_audit_log_entries() -> None:
    asyncio.run(_purge_old_audit_log_entries())


_NOTIFICATION_LOG_PURGE_ACTOR = "retention policy (automatic)"


async def _purge_old_notification_logs() -> None:
    """Delete `NotificationLog` rows older than `AppSettings.
    notification_log_retention_days` — same shape as
    `_purge_old_audit_log_entries` above, including being skipped
    entirely when retention is unset (`None` = keep forever). This is
    delivery history only; the `notification_rule.*` audit trail for
    rule/template changes is a separate table with its own retention."""
    async with db_session.AsyncSessionLocal() as session:
        app_settings = await get_or_create_app_settings(session)
        retention_days = app_settings.notification_log_retention_days
        if not retention_days:
            return

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        count_result = await session.execute(
            select(func.count())
            .select_from(NotificationLog)
            .where(NotificationLog.sent_at < cutoff)
        )
        deleted_count = count_result.scalar_one()
        if not deleted_count:
            return

        await session.execute(delete(NotificationLog).where(NotificationLog.sent_at < cutoff))
        await session.commit()

        await log_event(
            session,
            actor=_NOTIFICATION_LOG_PURGE_ACTOR,
            action="notification_logs.purge",
            summary=(
                f"Purged {deleted_count} notification log entr"
                f"{'ies' if deleted_count != 1 else 'y'} older than {retention_days} day(s)"
            ),
            details={"deleted_count": deleted_count, "retention_days": retention_days},
        )


@celery_app.task(name="app.tasks.jobs.purge_old_notification_logs")
def purge_old_notification_logs() -> None:
    asyncio.run(_purge_old_notification_logs())
