"""The Status page (`/status`): is the background machinery working?

- every periodic job (Celery Beat's schedule): when it last ran, how it
  ended, its last failure, and roughly when it runs next (`JobRun`,
  recorded by `app.tasks.job_runs.tracked`);
- whether a worker answers right now (one Celery ping, 1 s);
- each Fabric's last inventory sync (only Fabrics the account may see);
- release assessments: queued, failed, and the latest errors (the AI
  provider, a source that couldn't be read).

Read-only, `settings.view`: it shows how the instance runs, not anything a
Fabric holds — the Fabric rows are still scoped like everywhere else.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from celery.schedules import crontab
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_permission
from app.core.app_settings import get_or_create_app_settings
from app.db.models.job_run import JobRun
from app.db.models.release import Release
from app.db.models.role import Permission
from app.db.models.user import User
from app.db.session import get_db
from app.services.access_scope import fabrics_visible_to
from app.tasks.celery_app import celery_app
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/status", dependencies=[Depends(require_permission(Permission.SETTINGS_VIEW))]
)

# Shown on the page; older errors are on each release's own page.
ERRORS_SHOWN = 10
PING_TIMEOUT_SECONDS = 1.0


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def next_run(
    schedule: Any, last_started: datetime | None, interval: timedelta | None, now: datetime
) -> datetime | None:
    """Roughly when Beat fires the entry next. `interval` overrides a
    `timedelta` entry's own (the web process builds its schedule with the
    defaults; Beat reads the configured value at its start)."""
    try:
        if isinstance(schedule, crontab):
            # `remaining_delta`, not `remaining_estimate`: the latter measures
            # from Celery's own clock, not from `now`.
            start, delta, _ = schedule.remaining_delta(last_started or now)
            upcoming: datetime = start + delta
            if upcoming < now:
                start, delta, _ = schedule.remaining_delta(now)
                upcoming = start + delta
            # Celery's estimate lands a few seconds short of the minute.
            if upcoming.second or upcoming.microsecond:
                upcoming = upcoming.replace(second=0, microsecond=0) + timedelta(minutes=1)
            return upcoming
        every = interval or getattr(schedule, "run_every", None)
        if every is None or last_started is None:
            return None
        upcoming = last_started + every
        return upcoming if upcoming > now else now
    except Exception:  # noqa: BLE001 — an estimate, never worth a 500
        return None


def _ping_workers() -> int:
    replies = celery_app.control.ping(timeout=PING_TIMEOUT_SECONDS) or []
    return len(replies)


async def workers_online() -> int | None:
    """How many workers answer a ping; `None` when the broker can't be
    reached at all."""
    try:
        return await asyncio.to_thread(_ping_workers)
    except Exception:  # noqa: BLE001
        logger.info("Worker ping failed", exc_info=True)
        return None


@router.get("")
async def status_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    now = datetime.now(UTC)
    app_settings = await get_or_create_app_settings(db)
    runs = {row.name: row for row in (await db.execute(select(JobRun))).scalars()}
    intervals = {
        "refresh-release-catalog": timedelta(hours=app_settings.release_catalog_interval_hours),
    }
    jobs = []
    for name, entry in celery_app.conf.beat_schedule.items():
        run = runs.get(name)
        started = _aware(run.last_started_at) if run else None
        jobs.append(
            {
                "name": name,
                "run": run,
                "started": started,
                "finished": _aware(run.last_finished_at) if run else None,
                "error_at": _aware(run.last_error_at) if run else None,
                "running": bool(
                    run
                    and run.last_started_at
                    and (run.last_finished_at is None or run.last_finished_at < run.last_started_at)
                ),
                "next": next_run(entry["schedule"], started, intervals.get(name), now),
            }
        )

    fabrics = sorted(
        (await db.execute(await fabrics_visible_to(db, user))).scalars(),
        key=lambda f: f.name.lower(),
    )

    queued = await db.scalar(
        select(func.count())
        .select_from(Release)
        .where(Release.assessment_queued_at.is_not(None), Release.assessed_at.is_(None))
    )
    failed = list(
        (
            await db.execute(
                select(Release)
                .where(Release.assessment_error.is_not(None))
                .order_by(Release.updated_at.desc())
                .limit(ERRORS_SHOWN)
            )
        ).scalars()
    )
    failed_count = await db.scalar(
        select(func.count()).select_from(Release).where(Release.assessment_error.is_not(None))
    )
    last_assessed = await db.scalar(select(func.max(Release.assessed_at)))
    return templates.TemplateResponse(
        request,
        "status/page.html",
        {
            "jobs": jobs,
            "workers": await workers_online(),
            "fabrics": fabrics,
            "queued": queued or 0,
            "failed": failed,
            "failed_count": failed_count or 0,
            "last_assessed": _aware(last_assessed),
        },
    )
