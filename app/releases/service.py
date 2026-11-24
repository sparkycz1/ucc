"""The release pieces the jobs and web routes share: which model assesses,
gathering evidence from every configured source, refreshing the newest
community releases, and making sure every pending update has a `Release`
row."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig
from app.db.models.app_settings import AppSettings, WebSearchKind
from app.db.models.console import Console
from app.db.models.fabric import Fabric
from app.db.models.release import LatestRelease, Release
from app.db.models.user import User
from app.releases.evidence import Evidence, SourceError
from app.releases.products import APP_TITLES, search_term, version_key
from app.releases.sources.community import CommunityClient, matching_posts
from app.releases.sources.reddit import RedditClient
from app.releases.sources.web import WebSearchClient
from app.releases.verdicts import (
    TargetVerdict,
    UpdateTarget,
    installed_keys,
    load_latest,
    load_releases,
    max_risk_for,
    update_targets,
    verdicts,
)

logger = logging.getLogger(__name__)

# How long a "newest community release" lookup is trusted. Several Fabrics
# syncing every 15 minutes share it, so community.ui.com sees one lookup
# per product line per this period, not one per sync.
LATEST_MAX_AGE = timedelta(hours=6)
# At most this many lookups per discovery run — a first sync of a big
# Fabric fills the rest in over the next runs.
LATEST_LOOKUPS_PER_RUN = 40


async def assessment_model(
    db: AsyncSession, app_settings: AppSettings
) -> tuple[AiProviderConfig, AiModel] | None:
    """The configured model, if it and its provider are still enabled."""
    if app_settings.release_assessment_model_id is None:
        return None
    model = await db.get(AiModel, app_settings.release_assessment_model_id)
    if model is None or not model.enabled:
        return None
    provider = await db.get(AiProviderConfig, model.provider_id)
    if provider is None or not provider.enabled:
        return None
    return provider, model


def product_label(release: Release) -> str:
    """How people name it in a post: the community title, or our best guess."""
    return release.title or search_term(release.kind, release.product)


async def gather_evidence(app_settings: AppSettings, release: Release) -> Evidence:
    """Read every configured source. A failing source is noted in
    `Evidence.errors`, never raised — community.ui.com alone is still worth
    an assessment."""
    evidence = Evidence()
    community = CommunityClient()
    try:
        await community.collect(evidence, release.kind, release.product, release.version)
    except SourceError as exc:
        evidence.errors.append(str(exc))
    finally:
        await community.aclose()

    label = evidence.release.title if evidence.release is not None else product_label(release)
    if app_settings.reddit_client_id and app_settings.reddit_client_secret_encrypted:
        reddit = RedditClient(
            app_settings.reddit_client_id,
            decrypt_secret(app_settings.reddit_client_secret_encrypted),
        )
        try:
            await reddit.collect(evidence, label, release.version)
        except SourceError as exc:
            evidence.errors.append(str(exc))
        finally:
            await reddit.aclose()

    if app_settings.web_search_kind != WebSearchKind.NONE.value:
        web = WebSearchClient(
            app_settings.web_search_kind,
            url=app_settings.web_search_url,
            api_key=(
                decrypt_secret(app_settings.web_search_api_key_encrypted)
                if app_settings.web_search_api_key_encrypted
                else None
            ),
        )
        try:
            await web.collect(evidence, label, release.version)
        except SourceError as exc:
            evidence.errors.append(str(exc))
        finally:
            await web.aclose()
    return evidence


async def refresh_latest(
    db: AsyncSession, keys: set[tuple[str, str, str]], *, now: datetime | None = None
) -> int:
    """Look up the newest community release for every (kind, product,
    installed version) not checked within `LATEST_MAX_AGE`. Does not commit.
    Returns how many were looked up."""
    now = now or datetime.now(UTC)
    existing = await load_latest(db, keys)
    stale = [
        key
        for key in sorted(keys)
        if key not in existing or _aware(existing[key].checked_at) < now - LATEST_MAX_AGE
    ][:LATEST_LOOKUPS_PER_RUN]
    if not stale:
        return 0
    community = CommunityClient()
    try:
        for kind, product, current in stale:
            try:
                post = await community.latest_release(kind, product, current)
            except SourceError as exc:
                logger.info("Latest release lookup failed for %s %s: %s", kind, product, exc)
                break
            row = existing.get((kind, product, current))
            if row is None:
                row = LatestRelease(kind=kind, product=product, current_version=current)
                db.add(row)
            row.version = post.version if post else None
            row.title = post.title if post else None
            row.url = post.url if post else None
            row.checked_at = now
    finally:
        await community.aclose()
    return len(stale)


def _aware(value: datetime) -> datetime:
    # SQLite (tests) hands timezone-aware columns back naive.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def ensure_releases(db: AsyncSession, targets: Iterable[UpdateTarget]) -> list[Release]:
    """A `Release` row for every target version that has none yet. Does not
    commit; returns the new rows (flushed, so they have ids)."""
    targets = list(targets)
    existing = await load_releases(db, targets)
    created: dict[tuple[str, str, str], Release] = {}
    for target in targets:
        key = target.key
        if key is None or key in existing or key in created:
            continue
        created[key] = Release(kind=key[0], product=key[1], version=key[2])
        db.add(created[key])
    if created:
        await db.flush()
    return list(created.values())


# --- The release catalog (the Releases page) ---------------------------------
#
# Every UniFi application, whether or not anything synced runs it, with its
# newest few versions on community.ui.com — rated like any other release, so
# the Releases page answers "is the new Protect any good?" before a console
# even offers it.

CATALOG_VERSIONS = 3
CATALOG_REFRESH_AFTER = timedelta(hours=6)


def catalog_products() -> list[tuple[str, str]]:
    """(application key, community title) for every catalogued application."""
    return list(APP_TITLES.items())


async def refresh_catalog(db: AsyncSession, *, now: datetime | None = None) -> list[Release]:
    """Read each application's newest release posts from community.ui.com
    and upsert them as `Release` rows (kind=app). Does not commit. Returns
    the catalogue's current rows (newest `CATALOG_VERSIONS` per
    application). A community.ui.com failure stops the refresh, leaving the
    existing rows as they are."""
    now = now or datetime.now(UTC)
    community = CommunityClient()
    try:
        for product, title in catalog_products():
            try:
                items = await community.search(title, 30)
            except SourceError as exc:
                logger.info("Release catalog refresh failed at %s: %s", product, exc)
                break
            posts = sorted(
                matching_posts("app", product, items),
                key=lambda p: version_key(p.version) or (),
                reverse=True,
            )[:CATALOG_VERSIONS]
            for post in posts:
                release = (
                    await db.execute(
                        select(Release).where(
                            Release.kind == "app",
                            Release.product == product,
                            Release.version == post.version,
                        )
                    )
                ).scalar_one_or_none()
                if release is None:
                    release = Release(kind="app", product=product, version=post.version)
                    db.add(release)
                release.title = post.title
                release.community_release_id = post.id
                release.community_url = post.url
                release.stage = post.stage
                if release.published_at is None:
                    release.published_at = parse_time(post.created_at)
    finally:
        await community.aclose()
    await db.flush()
    return [r for rows in (await catalog(db)).values() for r in rows]


async def catalog(db: AsyncSession) -> dict[str, list[Release]]:
    """Application key → its newest `CATALOG_VERSIONS` releases with a
    community.ui.com post, newest first, in `catalog_products` order."""
    products = [product for product, _title in catalog_products()]
    rows = (
        await db.execute(
            select(Release).where(
                Release.kind == "app",
                Release.product.in_(products),
                Release.community_release_id.is_not(None),
            )
        )
    ).scalars()
    by_product: dict[str, list[Release]] = {product: [] for product in products}
    for release in rows:
        by_product[release.product].append(release)
    return {
        product: sorted(
            releases, key=lambda r: version_key(r.version) or (), reverse=True
        )[:CATALOG_VERSIONS]
        for product, releases in by_product.items()
        if releases
    }


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def claim_unrated(releases: Iterable[Release], *, now: datetime | None = None) -> list[str]:
    """Ids of `releases` never rated (and no human level) that aren't
    already queued, marked as queued. Several Fabrics syncing at once, or
    the catalogue refresh, all see the same new release; the mark keeps it
    to one assessment (and an assessment that keeps failing to one retry
    per `REQUEUE_AFTER`). The caller commits."""
    now = now or datetime.now(UTC)
    claimed = []
    seen: set[str] = set()
    for release in releases:
        if str(release.id) in seen:
            continue
        seen.add(str(release.id))
        if release.assessed_at is not None or release.manual_risk_level is not None:
            continue
        queued = release.assessment_queued_at
        if queued is not None and _aware(queued) > now - REQUEUE_AFTER:
            continue
        release.assessment_queued_at = now
        claimed.append(str(release.id))
    return claimed


REQUEUE_AFTER = timedelta(hours=1)


async def visible_verdicts(db: AsyncSession, user: User) -> list[tuple[Fabric, TargetVerdict]]:
    """Every pending update in the Fabrics `user` may see
    (`app.services.access_scope`), with its verdict — shared by the
    Releases/Assessments pages and the AI assistant's tools."""
    from app.core.app_settings import get_or_create_app_settings
    from app.services.access_scope import fabrics_visible_to

    app_settings = await get_or_create_app_settings(db)
    query = await fabrics_visible_to(db, user)
    fabrics = list(
        (
            await db.execute(
                query.options(
                    selectinload(Fabric.consoles).selectinload(Console.apps),
                    selectinload(Fabric.consoles).selectinload(Console.devices),
                ).order_by(Fabric.name)
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[Fabric, TargetVerdict]] = []
    for fabric in fabrics:
        consoles = list(fabric.consoles)
        latest = await load_latest(db, installed_keys(consoles))
        targets = update_targets(consoles, latest)
        releases = await load_releases(db, targets)
        max_level = max_risk_for(fabric, app_settings.max_acceptable_risk_level)
        out.extend((fabric, v) for v in verdicts(targets, releases, max_level))
    return out
