"""The post-login landing page: a read-only overview aggregated from the
areas the current user has permission to see. No `Permission` gate at the
router level (unlike every other page) — anyone with a valid session gets
*a* dashboard, just with fewer sections, mirroring how `base.html`'s nav
already hides links a role can't use.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models.audit_log import AuditLogEntry, AuditOutcome
from app.db.models.console import Console
from app.db.models.device import Device, device_has_update
from app.db.models.fabric import Fabric
from app.db.models.role import Permission
from app.db.models.scheduled_task import ScheduledTask
from app.db.models.user import User
from app.db.session import get_db
from app.scheduling.targets import task_within_scope
from app.services.access_scope import allowed_fabric_ids
from app.web.templating import templates

router = APIRouter()


async def _inventory_stats(db: AsyncSession, scope: set[uuid.UUID] | None) -> dict[str, int]:
    """Counts across every Fabric `scope` allows (`None` = all)."""

    def scoped(query: Select[Any]) -> Select[Any]:
        return query if scope is None else query.where(Console.fabric_id.in_(scope))

    fabric_query = select(func.count()).select_from(Fabric)
    failing_query = (
        select(func.count()).select_from(Fabric).where(Fabric.last_sync_error.is_not(None))
    )
    if scope is not None:
        fabric_query = fabric_query.where(Fabric.id.in_(scope))
        failing_query = failing_query.where(Fabric.id.in_(scope))
    consoles = scoped(select(func.count()).select_from(Console))
    devices = scoped(
        select(func.count()).select_from(Device).join(Console, Device.console_id == Console.id)
    )
    return {
        "fabrics": (await db.execute(fabric_query)).scalar_one(),
        "fabrics_failing": (await db.execute(failing_query)).scalar_one(),
        "consoles": (await db.execute(consoles)).scalar_one(),
        "consoles_with_update": (
            await db.execute(consoles.where(Console.os_update_available.is_not(None)))
        ).scalar_one(),
        "consoles_with_auto_update": (
            await db.execute(consoles.where(Console.auto_update_schedule.is_not(None)))
        ).scalar_one(),
        "devices": (await db.execute(devices)).scalar_one(),
        "devices_offline": (
            await db.execute(devices.where(Device.is_online.is_(False)))
        ).scalar_one(),
        "devices_with_update": (
            await db.execute(devices.where(device_has_update()))
        ).scalar_one(),
    }


@router.get("/dashboard")
async def show_dashboard(
    request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    context: dict[str, object] = {}
    if user.has_permission(Permission.FABRIC_VIEW):
        context["inventory_stats"] = await _inventory_stats(db, await allowed_fabric_ids(db, user))

    if user.has_permission(Permission.SCHEDULING_VIEW):
        # Filtered the same way `/scheduling` filters its own list — a
        # restricted account is shown only the schedules it could open.
        # Fetched unlimited then sliced, since the scope filter runs in
        # Python (see `app/web/routes/scheduling.py` for why).
        result = await db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.is_enabled.is_(True))
            .order_by(ScheduledTask.next_run_at.is_(None), ScheduledTask.next_run_at.asc())
        )
        enabled_tasks = [
            task for task in result.scalars().all() if await task_within_scope(db, user, task)
        ]
        context["upcoming_tasks"] = enabled_tasks[:5]
        context["enabled_task_count"] = len(enabled_tasks)

    # Deliberately never scoped: `audit.view` is a single global permission
    # over the whole deployment's audit trail — a per-Fabric audit view would
    # be a worse security control than none. See wiki/Architecture.md.
    if user.has_permission(Permission.AUDIT_VIEW):
        result = await db.execute(
            select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc()).limit(8)
        )
        context["recent_audit_entries"] = list(result.scalars().all())
        context["recent_denied_count"] = (
            await db.execute(
                select(func.count())
                .select_from(AuditLogEntry)
                .where(AuditLogEntry.outcome == AuditOutcome.DENIED)
            )
        ).scalar_one()

    return templates.TemplateResponse(request, "dashboard/index.html", context)
