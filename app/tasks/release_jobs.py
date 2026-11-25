"""Background jobs for release assessment (`app.releases`).

Same two-part shape and fork-safety rules as `app.tasks.jobs` (see its
module docstring): the real work is `async def _x(...)`, the Celery task is
a one-line `asyncio.run(...)` wrapper, sessions come from
`db_session.AsyncSessionLocal()` through the module.

- `discover_releases(fabric_id)` — enqueued after every successful
  inventory sync: refreshes the newest community releases for what is
  installed, creates a `Release` row for every pending update, and enqueues
  one `assess_release` per release never assessed.
- `assess_release(release_id)` — gather evidence, ask the model, store
  the rating, notify.
- `reassess_releases()` — daily Beat sweep: early ratings are made on
  little feedback, so a release with pending updates (or in the Releases
  catalogue) is re-rated once a day for its first 30 days.
- `refresh_release_catalog()` — Beat (daily by default, at Beat start,
  and the Releases page's Refresh button): the newest three
  versions of every UniFi application from community.ui.com, for the
  Releases page, each new one enqueued for assessment.

Unattended, so not audit-logged (CLAUDE.md); a human asking for a
re-assessment is audit-logged by the web route that enqueues it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.ai.base import AiProviderError
from app.ai.providers import build_client
from app.ai.usage import check_within_limits, record_usage
from app.core.app_settings import get_or_create_app_settings
from app.core.config import get_settings
from app.db import session as db_session
from app.db.models.console import Console
from app.db.models.fabric import Fabric
from app.db.models.notification_rule import NotificationEventType
from app.db.models.release import Release
from app.releases.assess import AssessmentError, assess, build_prompt
from app.releases.service import (
    assessment_model,
    catalog,
    claim_unrated,
    ensure_releases,
    gather_evidence,
    parse_time,
    product_label,
    refresh_catalog,
    refresh_latest,
)
from app.releases.verdicts import (
    UpdateTarget,
    Verdict,
    installed_keys,
    load_latest,
    load_releases,
    max_risk_for,
    update_targets,
    verdict_for,
)
from app.services.live_updates import KIND_UPDATES, publish_fabric_event
from app.services.notifications import notify
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Evidence from three sources plus a model call — well past the default
# 60 s task limit (`app.tasks.celery_app`).
ASSESSMENT_TIME_LIMIT_SECONDS = 300
REASSESS_AFTER = timedelta(hours=24)
REASSESS_WINDOW = timedelta(days=30)


async def load_consoles(session: Any, fabric_id: uuid.UUID) -> list[Console]:
    """A Fabric's consoles with apps and devices loaded."""
    result = await session.execute(
        select(Console)
        .where(Console.fabric_id == fabric_id)
        .options(selectinload(Console.apps), selectinload(Console.devices))
    )
    return list(result.scalars().all())


async def _claim_unrated(session: Any, targets: list[UpdateTarget]) -> list[str]:
    """The targets' releases, claimed for assessment (`claim_unrated`)."""
    keys = {t.key for t in targets if t.key is not None}
    releases = [
        release
        for release in (await load_releases(session, targets)).values()
        if (release.kind, release.product, release.version) in keys
    ]
    return claim_unrated(releases)


async def _discover_releases(fabric_id: str) -> dict[str, Any]:
    async with db_session.AsyncSessionLocal() as session:
        fabric = await session.get(Fabric, uuid.UUID(fabric_id))
        if fabric is None:
            return {"ok": False, "error": "Fabric not found."}
        consoles = await load_consoles(session, fabric.id)
        keys = installed_keys(consoles)
        looked_up = await refresh_latest(session, keys)
        targets = update_targets(consoles, await load_latest(session, keys))
        created = await ensure_releases(session, targets)
        await session.commit()

        app_settings = await get_or_create_app_settings(session)
        configured = await assessment_model(session, app_settings) is not None
        to_assess: list[str] = []
        if configured:
            to_assess = await _claim_unrated(session, targets)
            await session.commit()
    for release_id in to_assess:
        assess_release.delay(release_id)
    if looked_up or created:
        await publish_fabric_event(fabric_id, KIND_UPDATES)
    return {"ok": True, "created": len(created), "latest_lookups": looked_up}


@celery_app.task(name="app.tasks.release_jobs.discover_releases")
def discover_releases(fabric_id: str) -> dict[str, Any]:
    return asyncio.run(_discover_releases(fabric_id))


async def _fabrics_pending(session: Any, release: Release) -> list[Fabric]:
    """Enabled Fabrics with a pending update to `release`."""
    fabrics = list(
        (await session.execute(select(Fabric).where(Fabric.is_enabled.is_(True)))).scalars()
    )
    out = []
    for fabric in fabrics:
        consoles = await load_consoles(session, fabric.id)
        latest = await load_latest(session, installed_keys(consoles))
        if any(
            t.key == (release.kind, release.product, release.version)
            for t in update_targets(consoles, latest)
        ):
            out.append(fabric)
    return out


async def _assess_release(release_id: str) -> dict[str, Any]:
    async with db_session.AsyncSessionLocal() as session:
        release = await session.get(Release, uuid.UUID(release_id))
        if release is None:
            return {"ok": False, "error": "Release not found."}
        app_settings = await get_or_create_app_settings(session)
        configured = await assessment_model(session, app_settings)
        if configured is None:
            return {"ok": False, "error": "Release assessment is not configured."}
        provider, model = configured

        limit_error = await check_within_limits(session, app_settings)
        if limit_error is not None:
            release.assessment_error = limit_error
            await session.commit()
            return {"ok": False, "error": limit_error}

        evidence = await gather_evidence(app_settings, release)
        if evidence.release is not None:
            release.title = evidence.release.title
            release.community_release_id = evidence.release.id
            release.community_url = evidence.release.url
            release.stage = evidence.release.stage
            release.published_at = parse_time(evidence.release.created_at)

        prompt = build_prompt(
            label=product_label(release),
            version=release.version,
            stage=release.stage,
            published_at=release.published_at.date().isoformat() if release.published_at else None,
            evidence=evidence,
        )
        previous_level = release.effective_risk_level
        try:
            client = build_client(provider)
            outcome = await assess(client, model.model_id, prompt, get_settings().default_locale)
        except (AiProviderError, AssessmentError) as exc:
            release.assessment_error = str(exc)[:2000]
            await session.commit()
            logger.warning("assess_release(%s) failed: %s", release_id, exc)
            return {"ok": False, "error": str(exc)}

        await record_usage(
            session,
            user_id=None,
            provider_kind=provider.kind,
            model_id=model.model_id,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )
        result = outcome.result
        release.risk_level = result.risk_level
        release.reason = result.reason or None
        release.summary = result.summary
        release.issues = [issue.model_dump() for issue in result.issues]
        release.sources = evidence.sources()
        release.assessed_at = datetime.now(UTC)
        release.assessed_by_model = f"{provider.kind.value}/{model.model_id}"
        release.assessment_error = (
            ("Sources not read: " + "; ".join(evidence.errors))[:2000] if evidence.errors else None
        )
        await session.commit()

        label = product_label(release)
        details = f"{result.reason}\n\n{result.summary}" if result.reason else result.summary
        await notify(
            session,
            NotificationEventType.RELEASE_ASSESSED,
            context={
                "product": label,
                "version": release.version,
                "risk_level": str(release.effective_risk_level),
                "details": details,
            },
        )
        fabrics = await _fabrics_pending(session, release)
        for fabric in fabrics:
            max_level = max_risk_for(fabric, app_settings.max_acceptable_risk_level)
            now_stop = verdict_for(release, max_level) is Verdict.STOP
            was_stop = previous_level is not None and previous_level > max_level
            if now_stop and not was_stop:
                await notify(
                    session,
                    NotificationEventType.UPDATE_BLOCKED,
                    fabric=fabric,
                    context={
                        "target_name": f"{label} ({fabric.name})",
                        "version": release.version,
                        "risk_level": str(release.effective_risk_level),
                        "max_risk_level": str(max_level),
                        "details": details,
                    },
                )
        fabric_ids = [str(f.id) for f in fabrics]
    for fabric_id in fabric_ids:
        await publish_fabric_event(fabric_id, KIND_UPDATES)
    return {"ok": True, "risk_level": result.risk_level}


@celery_app.task(
    name="app.tasks.release_jobs.assess_release", time_limit=ASSESSMENT_TIME_LIMIT_SECONDS
)
def assess_release(release_id: str) -> dict[str, Any]:
    return asyncio.run(_assess_release(release_id))


async def _reassess_releases(now: datetime | None = None) -> int:
    """Enqueue a re-assessment of every release that still has a pending
    update somewhere or is in the Releases catalogue, was last rated over a
    day ago, and is young enough for its community feedback to still be
    changing."""
    now = now or datetime.now(UTC)
    async with db_session.AsyncSessionLocal() as session:
        app_settings = await get_or_create_app_settings(session)
        if await assessment_model(session, app_settings) is None:
            return 0
        candidates = list(
            (
                await session.execute(
                    select(Release).where(
                        Release.allowed_at.is_(None),
                        Release.manual_risk_level.is_(None),
                        or_(
                            Release.assessed_at.is_(None),
                            Release.assessed_at < now - REASSESS_AFTER,
                        ),
                        or_(
                            Release.published_at.is_(None),
                            Release.published_at > now - REASSESS_WINDOW,
                        ),
                    )
                )
            ).scalars()
        )
        if not candidates:
            return 0
        pending: set[tuple[str, str, str]] = set()
        fabric_ids = (
            await session.execute(select(Fabric.id).where(Fabric.is_enabled.is_(True)))
        ).scalars()
        for fabric_id in list(fabric_ids):
            consoles = await load_consoles(session, fabric_id)
            latest = await load_latest(session, installed_keys(consoles))
            pending.update(t.key for t in update_targets(consoles, latest) if t.key is not None)
        # The Releases page's catalogue is rated even with nothing pending.
        for rows in (await catalog(session)).values():
            pending.update((r.kind, r.product, r.version) for r in rows)
        ids = [str(r.id) for r in candidates if (r.kind, r.product, r.version) in pending]
    for release_id in ids:
        assess_release.delay(release_id)
    return len(ids)


@celery_app.task(name="app.tasks.release_jobs.reassess_releases")
def reassess_releases() -> None:
    asyncio.run(_reassess_releases())


async def _refresh_release_catalog() -> dict[str, Any]:
    """Beat (every `release_catalog_interval_hours`), Beat start, or the
    Refresh button: the newest versions of every UniFi application
    from community.ui.com (the Releases page), each never-rated one
    enqueued for assessment when a model is configured."""
    async with db_session.AsyncSessionLocal() as session:
        rows = await refresh_catalog(session)
        app_settings = await get_or_create_app_settings(session)
        to_assess = (
            claim_unrated(rows) if await assessment_model(session, app_settings) else []
        )
        await session.commit()
    for release_id in to_assess:
        assess_release.delay(release_id)
    return {"ok": True, "catalog": len(rows), "queued": len(to_assess)}


@celery_app.task(name="app.tasks.release_jobs.refresh_release_catalog")
def refresh_release_catalog() -> dict[str, Any]:
    return asyncio.run(_refresh_release_catalog())
