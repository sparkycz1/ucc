"""Background jobs for release assessment (`app.releases`).

Same two-part shape and fork-safety rules as `app.tasks.jobs` (see its
module docstring): the real work is `async def _x(...)`, the Celery task is
a one-line `asyncio.run(...)` wrapper, sessions come from
`db_session.AsyncSessionLocal()` through the module.

- `discover_releases(fabric_id)` — enqueued after every successful
  inventory sync: refreshes the newest community releases for what is
  installed, creates a `Release` row for every pending update and every
  installed version, and enqueues one `assess_release` per release never
  assessed (one per community post).
- `assess_release(release_id)` — gather evidence, ask the model, store
  the rating (and copy it to the other models on the same post), append
  to the rating history, notify what turned bad (`notify_rating_changes`).
- `reassess_releases()` — daily Beat sweep: early ratings are made on
  little feedback, so a release with pending updates, installed somewhere
  or in the Releases catalogue is re-rated once a day for its first 30 days.
- `refresh_release_catalog()` — Beat (daily by default, at Beat start,
  and the Releases page's Refresh button): the newest three
  versions of every UniFi application from community.ui.com, for the
  Releases page, each new one enqueued for assessment.

The sweeps themselves are unattended, so not audit-logged (CLAUDE.md); a
human asking for a re-assessment is audit-logged by the web route that
enqueues it. Every assessment that calls the AI *is* logged
(`release.assess`), with the model, its tokens and how long it took — it
costs money, and that is what the operator asked to see per action.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.ai import slots as ai_slots
from app.ai.base import AiProviderError
from app.ai.fallback import model_chain
from app.ai.providers import build_client
from app.ai.usage import Purpose, check_within_limits, record_usage
from app.audit import log_event
from app.core.app_settings import get_or_create_app_settings
from app.core.config import get_settings
from app.db import session as db_session
from app.db.models.audit_log import AuditOutcome
from app.db.models.console import Console
from app.db.models.fabric import Fabric
from app.db.models.notification_rule import NotificationEventType
from app.db.models.release import RatingSource, Release
from app.releases.assess import AssessmentError, assess, build_prompt
from app.releases.context import contextual_risk
from app.releases.history import RatingSnapshot, record_rating
from app.releases.policy import global_policy
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
    share_rating,
    stage_wanted,
)
from app.releases.verdicts import (
    PolicyFor,
    UpdateTarget,
    auto_update_applies,
    installed_keys,
    installed_targets,
    load_latest,
    load_releases,
    policy_for,
    update_targets,
    visible_targets,
)
from app.services import activity
from app.services.live_updates import KIND_UPDATES, publish_fabric_event
from app.services.notifications import notify
from app.tasks.celery_app import celery_app
from app.tasks.job_runs import tracked

logger = logging.getLogger(__name__)

# Evidence from three sources plus a model call — and, when that fails,
# the fallback model's — each up to its provider's timeout (at most
# `MAX_AI_TIMEOUT_SECONDS`). Well past the default 60 s task limit
# (`app.tasks.celery_app`).
ASSESSMENT_TIME_LIMIT_SECONDS = 900
_ASSESS_ACTOR = "release assessment (automatic)"


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
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
        # What runs now is rated too, so a version that turns bad after it
        # was installed is flagged (`installed_targets`).
        targets += installed_targets(consoles)
        created = await ensure_releases(session, targets)
        await session.commit()

        app_settings = await get_or_create_app_settings(session)
        configured = await assessment_model(session, app_settings) is not None
        to_assess: list[str] = []
        if configured:
            # An update to a stage hidden for its console/device isn't rated
            # (`app.releases.policy`); one whose stage is still unknown is,
            # and `_assess_release` stops once the evidence says it's hidden.
            shown = visible_targets(
                targets, await load_releases(session, targets), policy_for(fabric, app_settings)
            )
            to_assess = await _claim_unrated(session, shown)
            await session.commit()
    for release_id in to_assess:
        assess_release.delay(release_id)
    if looked_up or created:
        await publish_fabric_event(fabric_id, KIND_UPDATES)
    return {"ok": True, "created": len(created), "latest_lookups": looked_up}


@celery_app.task(name="app.tasks.release_jobs.discover_releases")
def discover_releases(fabric_id: str) -> dict[str, Any]:
    return asyncio.run(_discover_releases(fabric_id))


# A task that finds every assessment slot taken comes back this much later
# (plus jitter, so a batch doesn't retry in lockstep).
SLOT_RETRY_SECONDS = 30


async def _assess_release(release_id: str, force: bool = False) -> dict[str, Any]:
    """Rate one release, in one of the `ai_max_concurrent_assessments`
    slots (`app.ai.slots`); with none free, re-enqueue itself a little
    later instead. `force` (a person asked for it) rates it even when its
    stage is hidden everywhere."""
    async with db_session.AsyncSessionLocal() as session:
        limit = (await get_or_create_app_settings(session)).ai_max_concurrent_assessments
    token = await ai_slots.acquire(limit, ASSESSMENT_TIME_LIMIT_SECONDS)
    if token is None:
        delay = SLOT_RETRY_SECONDS + random.randint(0, SLOT_RETRY_SECONDS)  # noqa: S311
        assess_release.apply_async((release_id, force), countdown=delay)
        return {"ok": True, "deferred": delay}
    try:
        return await _assess_release_now(release_id, force)
    finally:
        await ai_slots.release(token)


async def _assess_release_now(release_id: str, force: bool) -> dict[str, Any]:
    async with db_session.AsyncSessionLocal() as session:
        release = await session.get(Release, uuid.UUID(release_id))
        if release is None:
            return {"ok": False, "error": "Release not found."}
        queued = release.assessment_queued_at
        if (
            not force
            and release.assessed_at is not None
            and (queued is None or release.assessed_at >= queued)
        ):
            # Already rated since it was queued — a duplicate delivery (a
            # worker restart redelivers, "Refresh now" re-queues). Nothing
            # to spend tokens on.
            return {"ok": True, "skipped": "already assessed"}
        app_settings = await get_or_create_app_settings(session)
        chain = await model_chain(session, app_settings)
        if not chain:
            return {"ok": False, "error": "Release assessment is not configured."}
        started = time.monotonic()

        limit_error = await check_within_limits(session, app_settings)
        if limit_error is not None:
            release.assessment_error = limit_error
            await session.commit()
            return {"ok": False, "error": limit_error}

        siblings_before = await _sibling_snapshots(session, release)
        evidence = await gather_evidence(app_settings, release)
        if evidence.release is not None:
            release.title = evidence.release.title
            release.community_release_id = evidence.release.id
            release.community_url = evidence.release.url
            release.stage = evidence.release.stage
            release.published_at = parse_time(evidence.release.created_at)
        if not force and not await stage_wanted(session, release, app_settings):
            # Its stage is hidden wherever it would apply: keep what was
            # learned (the stage, so it isn't claimed again), skip the model,
            # and drop the queue mark — it isn't waiting for anything.
            release.assessment_queued_at = None
            await session.commit()
            return {"ok": True, "skipped": f"stage {release.stage} is hidden"}

        prompt = build_prompt(
            label=product_label(release),
            version=release.version,
            stage=release.stage,
            published_at=release.published_at.date().isoformat() if release.published_at else None,
            evidence=evidence,
        )
        before = {release.id: RatingSnapshot.of(release), **siblings_before}
        previous_level = release.effective_risk_level
        label = product_label(release)
        target = f"{label} {release.version}"
        outcome = None
        used = chain[0]
        failures: list[str] = []
        tokens = 0
        for candidate in chain:
            call_started = time.monotonic()
            spent = (0, 0)
            try:
                client = build_client(candidate.provider)
                outcome = await assess(
                    client, candidate.model.model_id, prompt, get_settings().default_locale
                )
                spent = (outcome.input_tokens, outcome.output_tokens)
            except AssessmentError as exc:
                spent = (exc.input_tokens, exc.output_tokens)
                failures.append(f"{candidate.label}: {exc}")
            except AiProviderError as exc:
                failures.append(f"{candidate.label}: {exc}")
            if any(spent):
                tokens += sum(spent)
                await record_usage(
                    session,
                    user_id=None,
                    provider_kind=candidate.provider.kind,
                    model_id=candidate.model.model_id,
                    input_tokens=spent[0],
                    output_tokens=spent[1],
                    purpose=Purpose.ASSESSMENT,
                    label=target,
                    duration_ms=_ms_since(call_started),
                    fallback=candidate.fallback,
                )
            if outcome is not None:
                used = candidate
                break
        duration_ms = _ms_since(started)
        if outcome is None:
            error = "; ".join(failures)
            release.assessment_error = error[:2000]
            await session.commit()
            logger.warning("assess_release(%s) failed: %s", release_id, error)
            await log_event(
                session,
                actor=_ASSESS_ACTOR,
                action="release.assess",
                summary=f"AI assessment of {target} failed",
                outcome=AuditOutcome.FAILURE,
                target_type="release",
                target_id=release.id,
                target_label=target,
                details={
                    "models": [c.label for c in chain],
                    "error": error[:1000],
                    "tokens": tokens,
                    "duration_ms": duration_ms,
                },
            )
            return {"ok": False, "error": error}

        result = outcome.result
        release.assessment_tokens = tokens
        release.assessment_duration_ms = duration_ms
        release.risk_level = result.risk_level
        release.base_risk_level = result.base_risk_level
        release.reason = result.reason or None
        release.summary = result.summary
        release.issues = [issue.model_dump() for issue in result.issues]
        release.sources = evidence.sources()
        release.assessed_at = datetime.now(UTC)
        release.assessed_by_model = used.label
        release.assessment_error = (
            ("Sources not read: " + "; ".join(evidence.errors))[:2000] if evidence.errors else None
        )
        siblings = await share_rating(session, release)
        for rated in [release, *siblings]:
            record_rating(session, rated, RatingSource.AI, release.assessed_by_model)
        await session.commit()
        await log_event(
            session,
            actor=_ASSESS_ACTOR,
            action="release.assess",
            summary=(
                f"AI rated {target} {result.risk_level}/5 with {used.label}"
                + (" (fallback model)" if used.fallback else "")
                + f" — {tokens} tokens, {duration_ms / 1000:.1f} s"
            ),
            target_type="release",
            target_id=release.id,
            target_label=target,
            details={
                "model": used.label,
                "fallback": used.fallback,
                "failed_first": failures,
                "risk_level": result.risk_level,
                "tokens": tokens,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "duration_ms": duration_ms,
                "shared_with": len(siblings),
            },
        )

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
        if previous_level is not None and previous_level != release.effective_risk_level:
            await notify(
                session,
                NotificationEventType.RELEASE_RISK_CHANGED,
                context={
                    "product": label,
                    "version": release.version,
                    "risk_level": str(release.effective_risk_level),
                    "previous_risk_level": str(previous_level),
                    "details": details,
                },
            )
        fabric_ids = await notify_rating_changes(
            session, [release, *siblings], before, app_settings, label=label, details=details
        )
    for fabric_id in fabric_ids:
        await publish_fabric_event(fabric_id, KIND_UPDATES)
    return {"ok": True, "risk_level": result.risk_level, "shared_with": len(siblings)}


async def _sibling_snapshots(session: Any, release: Release) -> dict[uuid.UUID, RatingSnapshot]:
    if not release.community_release_id:
        return {}
    rows = (
        await session.execute(
            select(Release).where(
                Release.kind == release.kind,
                Release.community_release_id == release.community_release_id,
                Release.id != release.id,
            )
        )
    ).scalars()
    return {row.id: RatingSnapshot.of(row) for row in rows}


def _stop(allowed: bool, level: int | None, max_level: int) -> bool:
    return not allowed and level is not None and level > max_level


def _turned_bad(
    target: UpdateTarget,
    by_key: dict[tuple[str, str, str], Release],
    before: dict[uuid.UUID, RatingSnapshot],
    max_level: int,
) -> tuple[bool, int | None]:
    """Whether `target` is STOP now (at its own acceptable `max_level`) but
    wasn't before the re-rating, and its level now (in context)."""
    release = by_key[target.key]  # type: ignore[index]
    old = before.get(release.id)
    now = contextual_risk(release, target.profile)
    was = (
        _stop(old.allowed, contextual_risk(old, target.profile).level, max_level)
        if old is not None
        else False
    )
    return _stop(release.is_allowed, now.level, max_level) and not was, now.level


async def notify_rating_changes(
    session: Any,
    releases: list[Release],
    before: dict[uuid.UUID, RatingSnapshot],
    app_settings: Any,
    *,
    label: str,
    details: str,
) -> list[str]:
    """After `releases` were (re-)rated: per enabled Fabric, notify what
    *turned* bad for its own consoles and devices (the level in context,
    `app.releases.context`, against the Fabric's acceptable level):

    - `update.blocked` — a pending update to it is now STOP;
    - `update.auto_update_risk` — and UniFi's own auto-update on that
      console would install it anyway;
    - `release.installed_at_risk` — it is the version running now.

    Returns the ids of the Fabrics it touches (for the live refresh)."""
    by_key = {(r.kind, r.product, r.version): r for r in releases}
    fabrics = list(
        (await session.execute(select(Fabric).where(Fabric.is_enabled.is_(True)))).scalars()
    )
    touched: list[str] = []
    for fabric in fabrics:
        consoles = await load_consoles(session, fabric.id)
        latest = await load_latest(session, installed_keys(consoles))
        resolve: PolicyFor = policy_for(fabric, app_settings)
        pending = visible_targets(
            [t for t in update_targets(consoles, latest) if t.key in by_key], by_key, resolve
        )
        running = [t for t in installed_targets(consoles) if t.key in by_key]
        if not pending and not running:
            continue
        touched.append(str(fabric.id))

        blocked: dict[uuid.UUID, list[tuple[UpdateTarget, int | None]]] = {}
        auto: list[tuple[UpdateTarget, int | None]] = []
        for target in pending:
            bad, level = _turned_bad(target, by_key, before, resolve(target).max_risk_level)
            if bad:
                blocked.setdefault(by_key[target.key].id, []).append((target, level))  # type: ignore[index]
                if auto_update_applies(target):
                    auto.append((target, level))
        at_risk: list[tuple[UpdateTarget, int | None]] = []
        for target in running:
            bad, level = _turned_bad(target, by_key, before, resolve(target).max_risk_level)
            if bad:
                at_risk.append((target, level))

        for items in blocked.values():
            target, level = items[0]
            names = ", ".join(t.label for t, _l in items[:10])
            await notify(
                session,
                NotificationEventType.UPDATE_BLOCKED,
                fabric=fabric,
                context={
                    "target_name": names if len(items) <= 10 else f"{names}, …",
                    "version": target.target_version or "",
                    "risk_level": str(level),
                    "max_risk_level": str(resolve(target).max_risk_level),
                    "details": details,
                },
            )
        for target, level in auto:
            await notify(
                session,
                NotificationEventType.AUTO_UPDATE_RISK,
                fabric=fabric,
                context={
                    "target_name": target.label,
                    "version": target.target_version or "",
                    "risk_level": str(level),
                    "max_risk_level": str(resolve(target).max_risk_level),
                    "schedule": target.console.auto_update_schedule or "",
                    "details": details,
                },
            )
        if at_risk:
            names = ", ".join(t.label for t, _l in at_risk[:10])
            await notify(
                session,
                NotificationEventType.INSTALLED_AT_RISK,
                fabric=fabric,
                context={
                    "product": label,
                    "target_name": names if len(at_risk) <= 10 else f"{names}, …",
                    "version": at_risk[0][0].target_version or "",
                    "risk_level": str(max(level or 0 for _t, level in at_risk)),
                    "max_risk_level": str(resolve(at_risk[0][0]).max_risk_level),
                    "count": str(len(at_risk)),
                    "details": details,
                },
            )
    return touched


@celery_app.task(
    name="app.tasks.release_jobs.assess_release", time_limit=ASSESSMENT_TIME_LIMIT_SECONDS
)
def assess_release(release_id: str, force: bool = False) -> dict[str, Any]:
    return asyncio.run(_assess_release(release_id, force))


async def _reassess_releases(now: datetime | None = None) -> int:
    """Enqueue a re-assessment of every release that still has a pending
    update somewhere, runs somewhere, or is in the Releases catalogue, was last rated over a
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
        enabled = (
            await session.execute(select(Fabric).where(Fabric.is_enabled.is_(True)))
        ).scalars()
        for fabric in list(enabled):
            consoles = await load_consoles(session, fabric.id)
            latest = await load_latest(session, installed_keys(consoles))
            targets = [*update_targets(consoles, latest), *installed_targets(consoles)]
            resolve = policy_for(fabric, app_settings)
            releases = await load_releases(session, targets)
            pending.update(
                t.key
                for t in targets
                if t.key is not None
                and not (t.key in releases and resolve(t).hides(releases[t.key].stage))
            )
        # The Releases page's catalogue is rated even with nothing pending
        # (what it shows: the globally hidden stages are left out).
        for group in await catalog(
            session, hidden_stages=global_policy(app_settings).hidden_stages
        ):
            pending.update((r.kind, r.product, r.version) for r in group.all_rows)
        # One re-assessment per community post: the models on a firmware
        # line share it (`share_rating`).
        ids = []
        posts: set[tuple[str, str]] = set()
        for release in candidates:
            if (release.kind, release.product, release.version) not in pending:
                continue
            if release.community_release_id:
                post = (release.kind, release.community_release_id)
                if post in posts:
                    continue
                posts.add(post)
            ids.append(str(release.id))
    for release_id in ids:
        assess_release.delay(release_id)
    return len(ids)


@celery_app.task(name="app.tasks.release_jobs.reassess_releases")
def reassess_releases() -> None:
    asyncio.run(tracked("reassess-releases", _reassess_releases()))


async def _refresh_release_catalog(assess_now: bool = False) -> dict[str, Any]:
    """Beat (every `release_catalog_interval_hours`), Beat start, or the
    Refresh button: the newest versions of every UniFi application
    from community.ui.com (the Releases page), each never-rated one
    enqueued for assessment when a model is configured. `assess_now` (the
    button) also re-queues the unrated ones already queued or failed
    within the last hour, instead of waiting for their retry."""
    async with db_session.AsyncSessionLocal() as session:
        app_settings = await get_or_create_app_settings(session)
        hidden = global_policy(app_settings)
        rows = await refresh_catalog(session, hidden_stages=hidden.hidden_stages)
        to_assess = (
            claim_unrated((r for r in rows if not hidden.hides(r.stage)), retry_now=assess_now)
            if await assessment_model(session, app_settings)
            else []
        )
        await session.commit()
    for release_id in to_assess:
        assess_release.delay(release_id)
    return {"ok": True, "catalog": len(rows), "queued": len(to_assess)}


@celery_app.task(name="app.tasks.release_jobs.refresh_release_catalog")
def refresh_release_catalog(assess_now: bool = False) -> dict[str, Any]:
    return asyncio.run(tracked("refresh-release-catalog", _refresh_catalog_marked(assess_now)))


async def _refresh_catalog_marked(assess_now: bool) -> dict[str, Any]:
    """`_refresh_release_catalog`, shown as running in the activity indicator."""
    async with activity.running("catalog", "catalog", 600):
        return await _refresh_release_catalog(assess_now)
