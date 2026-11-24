"""Resolving a `ScheduledTask`'s target into what it applies to right now —
shared by the job that executes a schedule (`app.scheduling.jobs`) and the
web UI.

Also the single-`<select>` encoding used by the "New/edit scheduled task"
form (`encode_target`/`decode_target`): one dropdown listing "All Fabrics",
every Fabric, and every site — simpler than a type radio plus conditionally
relevant selects, and needs no client-side JS.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.console import Console
from app.db.models.fabric import Fabric
from app.db.models.scheduled_task import ScheduledTask, ScheduleTargetType
from app.db.models.site import Site
from app.db.models.user import User
from app.services.access_scope import allowed_fabric_ids

_ALL_VALUE = "all"
_FABRIC_PREFIX = "fabric:"
_SITE_PREFIX = "site:"


@dataclass(frozen=True)
class ResolvedTarget:
    """What a scheduled action runs against. `sites` is `None` for a whole
    Fabric (every site under `fabrics`, including ones synced later) and a
    one-element list for a site target."""

    fabrics: list[Fabric] = field(default_factory=list)
    sites: list[Site] | None = None


def encode_target(
    target_type: ScheduleTargetType,
    target_fabric_id: uuid.UUID | None,
    target_site_id: uuid.UUID | None,
) -> str:
    if target_type == ScheduleTargetType.SITE and target_site_id is not None:
        return f"{_SITE_PREFIX}{target_site_id}"
    if target_type == ScheduleTargetType.FABRIC and target_fabric_id is not None:
        return f"{_FABRIC_PREFIX}{target_fabric_id}"
    return _ALL_VALUE


def decode_target(raw: str) -> tuple[ScheduleTargetType, uuid.UUID | None, uuid.UUID | None]:
    """Returns (type, fabric_id, site_id). Raises ValueError on anything
    malformed — the `<select>` is server-rendered, so a bad value means a
    tampered request. A site target's `fabric_id` is filled in by the
    caller from the site itself (`site_fabric_id`)."""
    if raw == _ALL_VALUE:
        return ScheduleTargetType.ALL_FABRICS, None, None
    if raw.startswith(_FABRIC_PREFIX):
        return ScheduleTargetType.FABRIC, uuid.UUID(raw[len(_FABRIC_PREFIX) :]), None
    if raw.startswith(_SITE_PREFIX):
        return ScheduleTargetType.SITE, None, uuid.UUID(raw[len(_SITE_PREFIX) :])
    raise ValueError(f'Invalid target "{raw}".')


async def site_fabric_id(db: AsyncSession, site_id: uuid.UUID) -> uuid.UUID | None:
    result = await db.execute(
        select(Console.fabric_id)
        .join(Site, Site.console_id == Console.id)
        .where(Site.id == site_id)
    )
    return result.scalar_one_or_none()


async def resolve_target(db: AsyncSession, task: ScheduledTask) -> ResolvedTarget:
    """Resolved fresh each time (never cached on the task) — the inventory
    under a Fabric changes with every sync. Disabled Fabrics are skipped."""
    if task.target_type == ScheduleTargetType.ALL_FABRICS:
        result = await db.execute(select(Fabric).where(Fabric.is_enabled.is_(True)))
        return ResolvedTarget(fabrics=list(result.scalars().all()))

    if task.target_type == ScheduleTargetType.FABRIC:
        fabric = await db.get(Fabric, task.target_fabric_id) if task.target_fabric_id else None
        if fabric is None or not fabric.is_enabled:
            return ResolvedTarget()
        return ResolvedTarget(fabrics=[fabric])

    if task.target_type == ScheduleTargetType.SITE and task.target_site_id is not None:
        site_result = await db.execute(
            select(Site)
            .options(selectinload(Site.console).selectinload(Console.fabric))
            .where(Site.id == task.target_site_id)
        )
        site = site_result.scalar_one_or_none()
        if site is None or not site.console.fabric.is_enabled:
            return ResolvedTarget(sites=[])
        return ResolvedTarget(fabrics=[site.console.fabric], sites=[site])

    return ResolvedTarget()


async def target_within_scope(
    db: AsyncSession,
    user: User,
    target_type: ScheduleTargetType,
    target_fabric_id: uuid.UUID | None,
    target_site_id: uuid.UUID | None,
) -> bool:
    """Whether `user`'s Fabric scope covers this target. Checked when a
    schedule is created/edited and to decide whether one is listed — never
    when it fires (a stored schedule has no "current user").

    `ALL_FABRICS` is refused outright for a restricted account: a stored
    target outlives the moment it was written, and "every Fabric" would
    silently start covering Fabrics the account can't see."""
    fabric_ids = await allowed_fabric_ids(db, user)
    if fabric_ids is None:
        return True
    if target_type == ScheduleTargetType.ALL_FABRICS:
        return False
    if target_type == ScheduleTargetType.FABRIC:
        return target_fabric_id is not None and target_fabric_id in fabric_ids
    if target_type == ScheduleTargetType.SITE:
        if target_site_id is None:
            return False
        return await site_fabric_id(db, target_site_id) in fabric_ids
    return False


async def task_within_scope(db: AsyncSession, user: User, task: ScheduledTask) -> bool:
    return await target_within_scope(
        db, user, task.target_type, task.target_fabric_id, task.target_site_id
    )
