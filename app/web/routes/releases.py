"""Releases and their assessments.

- **Releases** (`/releases`) — the catalogue: every UniFi application's
  newest versions from community.ui.com with their ratings, whether or not
  anything synced runs them.
- **Assessments** (`/assessments`) — every release a synced console or
  device could update to, its AI risk rating and the GO/STOP
  recommendation.
- A release's page (`/releases/{id}`) — the rating, its sources, the
  affected consoles and devices, and the human decisions on top of it.

- `fabric.view`: all of the above (only the pending updates in Fabrics the
  account may see are listed with a release), and any version's
  release-notes link.
- `release.manage`: refresh the catalogue now, ask for a re-assessment, set or clear a manual risk
  level (it replaces the AI's in every verdict).
- `update.override`: allow a release whatever its risk level (every
  pending update to it becomes GO), or withdraw that.

Releases themselves are public UniFi information, global to the instance;
what is scoped is which pending updates (consoles/devices) are shown.
ucc only recommends — it never runs an update (Site Manager has no
endpoint for it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_event
from app.auth.dependencies import get_current_user, require_permission
from app.core.app_settings import get_or_create_app_settings
from app.core.csrf import verify_csrf
from app.db.models.console import Console, ConsoleApp
from app.db.models.device import Device
from app.db.models.release import (
    MAX_RISK_LEVEL,
    MIN_RISK_LEVEL,
    RatingSource,
    Release,
    ReleaseKind,
)
from app.db.models.role import Permission
from app.db.models.user import User
from app.db.session import get_db
from app.releases.context import CATEGORIES
from app.releases.evidence import SourceError
from app.releases.history import rating_history, record_rating
from app.releases.products import community_search_url, version_key
from app.releases.service import (
    assessment_model,
    catalog,
    model_names,
    product_label,
    visible_verdicts,
)
from app.releases.sources.community import SITE_URL, CommunityClient
from app.releases.verdicts import TargetSource
from app.releases.versions import normalize_version
from app.services.access_scope import fabrics_visible_to
from app.tasks.release_jobs import assess_release, refresh_release_catalog
from app.web.templating import templates

router = APIRouter(
    prefix="/releases", dependencies=[Depends(require_permission(Permission.FABRIC_VIEW))]
)
# The ratings of everything with a pending update ("Assessments"); until
# 0.2.2 this list was `/releases`, which is now the catalogue.
assessments_router = APIRouter(
    prefix="/assessments", dependencies=[Depends(require_permission(Permission.FABRIC_VIEW))]
)
_manage = Depends(require_permission(Permission.RELEASE_MANAGE))
_override = Depends(require_permission(Permission.UPDATE_OVERRIDE))

_VALID_KINDS = {kind.value for kind in ReleaseKind}


@router.get("")
async def release_catalog(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Every UniFi application's newest versions from community.ui.com,
    and the UniFi OS / firmware lines of the hardware in the account's
    Fabrics, with their ratings and how many consoles/devices in those
    Fabrics run each version."""
    query = await fabrics_visible_to(db, user)
    visible_ids = [f.id for f in (await db.execute(query)).scalars()]
    installed: dict[tuple[str, str, str], int] = {}

    def count(kind: str, product: str | None, version: str | None) -> None:
        normalized = normalize_version(version)
        if product and normalized:
            key = (kind, product.strip(), normalized)
            installed[key] = installed.get(key, 0) + 1

    if visible_ids:
        scope = Console.fabric_id.in_(visible_ids)
        for name, version in (
            await db.execute(
                select(ConsoleApp.name, ConsoleApp.version)
                .join(Console, ConsoleApp.console_id == Console.id)
                .where(scope)
            )
        ).all():
            count(ReleaseKind.APP.value, name, version)
        for model, version in (
            await db.execute(select(Console.model, Console.os_version).where(scope))
        ).all():
            count(ReleaseKind.OS.value, model, version)
        for model, version in (
            await db.execute(
                select(Device.model, Device.version)
                .join(Console, Device.console_id == Console.id)
                .where(scope)
            )
        ).all():
            count(ReleaseKind.FIRMWARE.value, model, version)
    groups = await catalog(db, visible_ids)
    # Per shown row: how many run it, over every model on the line.
    running = {
        release.id: sum(installed.get((group.kind, p, release.version), 0) for p in group.products)
        for group in groups
        for release in group.releases
    }
    app_settings = await get_or_create_app_settings(db)
    return templates.TemplateResponse(
        request,
        "releases/catalog.html",
        {
            "catalog": groups,
            "running": running,
            "model_names": await model_names(db),
            "max_risk_level": app_settings.max_acceptable_risk_level,
            "assessment_configured": await assessment_model(db, app_settings) is not None,
            "refreshing": request.query_params.get("refreshing") == "1",
            "csrf_token": request.state.csrf_token,
        },
    )


@router.post("/refresh", dependencies=[_manage, Depends(verify_csrf)])
async def refresh_catalog_now(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Refresh the catalogue from community.ui.com now instead of waiting
    for Beat (`release_catalog_interval_hours`); new versions are queued
    for assessment by the task itself."""
    refresh_release_catalog.delay()
    await log_event(
        db,
        request=request,
        action="release.catalog.refresh",
        summary="Requested a refresh of the Releases catalogue",
    )
    return RedirectResponse(url="/releases?refreshing=1", status_code=status.HTTP_303_SEE_OTHER)


@assessments_router.get("")
async def list_assessments(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    pending = await visible_verdicts(db, user)
    counts: dict[uuid.UUID, dict[str, int]] = {}
    # The installed versions each release would replace — "6.0.11 → 6.0.17".
    current: dict[uuid.UUID, set[str]] = {}
    for _fabric, verdict in pending:
        if verdict.release is None:
            continue
        bucket = counts.setdefault(verdict.release.id, {"targets": 0, "stop": 0})
        bucket["targets"] += 1
        if verdict.verdict == "stop":
            bucket["stop"] += 1
        installed = normalize_version(verdict.target.current_version)
        if installed:
            current.setdefault(verdict.release.id, set()).add(installed)
    show_all = request.query_params.get("all") == "1"
    query = select(Release).order_by(Release.created_at.desc())
    if not show_all:
        query = query.where(Release.id.in_(list(counts)))
    releases = list((await db.execute(query.limit(500))).scalars().all())
    app_settings = await get_or_create_app_settings(db)
    return templates.TemplateResponse(
        request,
        "assessments/list.html",
        {
            "releases": releases,
            "counts": counts,
            "current_versions": {
                release_id: sorted(versions, key=lambda v: version_key(v) or ())
                for release_id, versions in current.items()
            },
            "show_all": show_all,
            "max_risk_level": app_settings.max_acceptable_risk_level,
            "assessment_configured": await assessment_model(db, app_settings) is not None,
            "unknown_count": sum(1 for _f, v in pending if v.target.target_version is None),
            "model_names": await model_names(db),
        },
    )


def _is_community_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(SITE_URL + "/")


@router.get("/notes")
async def release_notes(
    db: AsyncSession = Depends(get_db),
    kind: str = "",
    product: str = "",
    version: str = "",
) -> Response:
    """Redirect to the release notes of `kind`/`product`/`version` on
    community.ui.com — a stored match if there is one, else a live search,
    else community.ui.com's own release search. Only ever redirects to
    community.ui.com."""
    normalized = normalize_version(version)
    if kind not in _VALID_KINDS or not product.strip() or normalized is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown release.")
    product = product.strip()[:100]
    stored = (
        await db.execute(
            select(Release).where(
                Release.kind == kind, Release.product == product, Release.version == normalized
            )
        )
    ).scalar_one_or_none()
    target: str | None = stored.community_url if stored is not None else None
    if not _is_community_url(target):
        community = CommunityClient()
        try:
            post = await community.find_release(kind, product, normalized)
        except SourceError:
            post = None
        finally:
            await community.aclose()
        target = post.url if post is not None else None
        if post is not None and stored is not None:
            stored.title = stored.title or post.title
            stored.community_release_id = post.id
            stored.community_url = post.url
            stored.stage = stored.stage or post.stage
            await db.commit()
    if not _is_community_url(target):
        target = community_search_url(kind, product, normalized)
    return RedirectResponse(url=str(target), status_code=status.HTTP_302_FOUND)


async def _get_release_or_404(db: AsyncSession, release_id: uuid.UUID) -> Release:
    release = await db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found.")
    return release


@router.get("/{release_id}")
async def show_release(
    request: Request,
    release_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    release = await _get_release_or_404(db, release_id)
    affected = []
    running = []
    for fabric, verdict in await visible_verdicts(db, user, installed=True):
        if verdict.release is None or verdict.release.id != release.id:
            continue
        if verdict.target.source == TargetSource.INSTALLED:
            running.append((fabric, verdict))
        else:
            affected.append((fabric, verdict))
    app_settings = await get_or_create_app_settings(db)
    return templates.TemplateResponse(
        request,
        "releases/detail.html",
        {
            "release": release,
            "label": product_label(release),
            "affected": affected,
            "running": running,
            "history": await rating_history(db, release),
            "categories": CATEGORIES,
            "csrf_token": request.state.csrf_token,
            "assessment_configured": await assessment_model(db, app_settings) is not None,
            "queued": request.query_params.get("queued") == "1",
            "risk_levels": list(range(MIN_RISK_LEVEL, MAX_RISK_LEVEL + 1)),
            "model_names": await model_names(db),
        },
    )


def _back(release: Release, **query: str) -> RedirectResponse:
    suffix = "?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
    return RedirectResponse(
        url=f"/releases/{release.id}{suffix}", status_code=status.HTTP_303_SEE_OTHER
    )


def _label(release: Release) -> str:
    return f"{product_label(release)} {release.version}"


@router.post("/{release_id}/assess", dependencies=[_manage, Depends(verify_csrf)])
async def request_assessment(
    request: Request, release_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Response:
    release = await _get_release_or_404(db, release_id)
    app_settings = await get_or_create_app_settings(db)
    if await assessment_model(db, app_settings) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Release assessment is not configured (Settings → Assessments).",
        )
    release.assessment_queued_at = datetime.now(UTC)
    await db.commit()
    assess_release.delay(str(release.id))
    await log_event(
        db,
        request=request,
        action="release.assess.request",
        summary=f"Requested an AI assessment of {_label(release)}",
        target_type="release",
        target_id=release.id,
        target_label=_label(release),
    )
    return _back(release, queued="1")


@router.post("/{release_id}/risk", dependencies=[_manage, Depends(verify_csrf)])
async def set_manual_risk(
    request: Request,
    release_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    risk_level: str = Form(""),
    note: str = Form(""),
) -> Response:
    """Blank `risk_level` clears the manual level (back to the AI's)."""
    release = await _get_release_or_404(db, release_id)
    raw = risk_level.strip()
    if raw:
        if not raw.isdigit() or not MIN_RISK_LEVEL <= int(raw) <= MAX_RISK_LEVEL:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Risk level must be 1 to 5.",
            )
        release.manual_risk_level = int(raw)
        release.manual_note = note.strip()[:2000] or None
        release.manual_by = user.username
        release.manual_at = datetime.now(UTC)
        record_rating(db, release, RatingSource.MANUAL, user.username)
        action, summary = (
            "release.risk.set",
            f"Set the risk level of {_label(release)} to {raw}/5 by hand",
        )
    else:
        release.manual_risk_level = None
        release.manual_note = None
        release.manual_by = None
        release.manual_at = None
        record_rating(db, release, RatingSource.MANUAL_CLEARED, user.username)
        action, summary = (
            "release.risk.clear",
            f"Cleared the manual risk level of {_label(release)}",
        )
    await db.commit()
    await log_event(
        db,
        request=request,
        action=action,
        summary=summary,
        target_type="release",
        target_id=release.id,
        target_label=_label(release),
        details={"risk_level": release.manual_risk_level, "ai_risk_level": release.risk_level},
    )
    return _back(release)


@router.post("/{release_id}/allow", dependencies=[_override, Depends(verify_csrf)])
async def allow_release(
    request: Request,
    release_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    note: str = Form(""),
) -> Response:
    release = await _get_release_or_404(db, release_id)
    release.allowed_by = user.username
    release.allowed_at = datetime.now(UTC)
    release.allowed_note = note.strip()[:2000] or None
    record_rating(db, release, RatingSource.ALLOWED, user.username)
    await db.commit()
    await log_event(
        db,
        request=request,
        action="release.allow",
        summary=f"Allowed {_label(release)} regardless of its risk level",
        target_type="release",
        target_id=release.id,
        target_label=_label(release),
        details={"risk_level": release.effective_risk_level, "note": release.allowed_note},
    )
    return _back(release)


@router.post("/{release_id}/revoke", dependencies=[_override, Depends(verify_csrf)])
async def revoke_allow(
    request: Request,
    release_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    release = await _get_release_or_404(db, release_id)
    release.allowed_by = None
    release.allowed_at = None
    release.allowed_note = None
    record_rating(db, release, RatingSource.ALLOW_REVOKED, user.username)
    await db.commit()
    await log_event(
        db,
        request=request,
        action="release.allow.revoke",
        summary=f"Withdrew the manual allowance of {_label(release)}",
        target_type="release",
        target_id=release.id,
        target_label=_label(release),
    )
    return _back(release)
