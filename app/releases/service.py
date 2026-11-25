"""The release pieces the jobs and web routes share: which model assesses,
gathering evidence from every configured source, refreshing the newest
community releases, and making sure every pending update has a `Release`
row."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig
from app.db.models.app_settings import AppSettings, WebSearchKind
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.release import LatestRelease, Release, ReleaseKind
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
    installed_targets,
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


async def model_names(db: AsyncSession) -> dict[str, str]:
    """Model code → display name ("UDRULT" → "UCG-Ultra") from what is
    synced, for showing a release's `product`. Instance-wide like releases
    themselves: model names are public UniFi facts, not Fabric data."""
    names: dict[str, str] = {}
    for model_cls in (Console, Device):
        rows = await db.execute(
            select(model_cls.model, model_cls.model_name)
            .where(model_cls.model.is_not(None), model_cls.model_name.is_not(None))
            .distinct()
        )
        for code, name in rows.all():
            if code and name:
                names.setdefault(code, name)
    return names


CATALOG_VERSIONS = 3


def catalog_products() -> list[tuple[str, str]]:
    """(application key, community title) for every catalogued application."""
    return list(APP_TITLES.items())


async def _upsert_post(db: AsyncSession, kind: str, product: str, post: Any) -> Release:
    release = (
        await db.execute(
            select(Release).where(
                Release.kind == kind, Release.product == product, Release.version == post.version
            )
        )
    ).scalar_one_or_none()
    if release is None:
        release = Release(kind=kind, product=product, version=post.version)
        db.add(release)
    release.title = post.title
    release.community_release_id = post.id
    release.community_url = post.url
    release.stage = post.stage
    if release.published_at is None:
        release.published_at = parse_time(post.created_at)
    return release


async def installed_products(
    db: AsyncSession, fabric_ids: Iterable[uuid.UUID] | None = None
) -> set[tuple[str, str]]:
    """(kind, model) of every console (UniFi OS) and device (firmware) in
    `fabric_ids`, or in every enabled Fabric."""
    if fabric_ids is None:
        scope = Console.fabric_id.in_(select(Fabric.id).where(Fabric.is_enabled.is_(True)))
    else:
        scope = Console.fabric_id.in_(list(fabric_ids))
    out: set[tuple[str, str]] = set()
    for (model,) in (
        await db.execute(select(Console.model).where(scope, Console.model.is_not(None)).distinct())
    ).all():
        if model and model.strip():
            out.add((ReleaseKind.OS.value, model.strip()))
    for (model,) in (
        await db.execute(
            select(Device.model)
            .join(Console, Device.console_id == Console.id)
            # Not the console's own entry: its firmware is its UniFi OS.
            .where(scope, Device.model.is_not(None), Device.model != Console.model)
            .distinct()
        )
    ).all():
        if model and model.strip():
            out.add((ReleaseKind.FIRMWARE.value, model.strip()))
    return out


async def _line_titles(
    db: AsyncSession, products: set[tuple[str, str]]
) -> dict[tuple[str, str], list[str]]:
    """(kind, community title) → the installed models on that release line,
    from the `LatestRelease` lookups every sync already makes (a firmware
    line is the title of the installed version's post, so this costs
    community.ui.com nothing extra)."""
    if not products:
        return {}
    rows = (
        await db.execute(
            select(LatestRelease)
            .where(
                LatestRelease.kind.in_([ReleaseKind.OS.value, ReleaseKind.FIRMWARE.value]),
                LatestRelease.title.is_not(None),
            )
            .order_by(LatestRelease.checked_at)
        )
    ).scalars()
    title_of: dict[tuple[str, str], str] = {}
    for row in rows:
        if (row.kind, row.product) in products and row.title:
            title_of[(row.kind, row.product)] = row.title  # the newest lookup wins
    lines: dict[tuple[str, str], list[str]] = {}
    for (kind, product), title in sorted(title_of.items()):
        lines.setdefault((kind, title), []).append(product)
    return lines


async def refresh_catalog(db: AsyncSession) -> list[Release]:
    """Read the newest release posts from community.ui.com and upsert them
    as `Release` rows: every application (kind=app), and the UniFi OS and
    firmware lines of every console and device in an enabled Fabric (one
    row per installed model, so verdicts find them). Does not commit.
    Returns the catalogue's current rows. A community.ui.com failure stops
    the refresh, leaving the existing rows as they are."""
    lines = await _line_titles(db, await installed_products(db))
    community = CommunityClient()
    try:
        for product, title in catalog_products():
            try:
                items = await community.search(title, 30)
            except SourceError as exc:
                logger.info("Release catalog refresh failed at %s: %s", product, exc)
                return await _catalog_rows(db)
            posts = sorted(
                matching_posts("app", product, items),
                key=lambda p: version_key(p.version) or (),
                reverse=True,
            )[:CATALOG_VERSIONS]
            for post in posts:
                await _upsert_post(db, ReleaseKind.APP.value, product, post)
        for (kind, title), products in lines.items():
            try:
                items = await community.search(title, 30)
            except SourceError as exc:
                logger.info("Release catalog refresh failed at %s: %s", title, exc)
                break
            posts = sorted(
                [p for p in matching_posts(kind, products[0], items) if p.title == title],
                key=lambda p: version_key(p.version) or (),
                reverse=True,
            )[:CATALOG_VERSIONS]
            for product in products:
                for post in posts:
                    await _upsert_post(db, kind, product, post)
    finally:
        await community.aclose()
    return await _catalog_rows(db)


async def _catalog_rows(db: AsyncSession) -> list[Release]:
    await db.flush()
    return [r for group in await catalog(db) for r in group.all_rows]


@dataclass
class CatalogGroup:
    """One section of the Releases page: an application, or a UniFi OS /
    firmware release line with the installed models on it."""

    kind: str
    title: str
    # Application key, or the models on the line.
    products: list[str]
    # Newest first, one per version (the line's models share the post, so
    # they share the rating — `app.tasks.release_jobs` copies it).
    releases: list[Release]
    # Every row behind `releases` (one per model) — what gets rated.
    all_rows: list[Release] = field(default_factory=list)


def _newest(rows: Iterable[Release]) -> list[Release]:
    by_version: dict[str, Release] = {}
    for row in rows:
        current = by_version.get(row.version)
        # The rated (and most recently rated) row stands for its version.
        if current is None or (row.assessed_at or datetime.min.replace(tzinfo=UTC)) > (
            current.assessed_at or datetime.min.replace(tzinfo=UTC)
        ):
            by_version[row.version] = row
    return sorted(
        by_version.values(), key=lambda r: version_key(r.version) or (), reverse=True
    )[:CATALOG_VERSIONS]


async def catalog(
    db: AsyncSession, fabric_ids: Iterable[uuid.UUID] | None = None
) -> list[CatalogGroup]:
    """The Releases page: every application's newest `CATALOG_VERSIONS`
    releases with a community.ui.com post (in `catalog_products` order),
    then the UniFi OS and firmware lines of what is installed in
    `fabric_ids` (default: every enabled Fabric) — a scoped viewer sees
    only the lines of hardware in Fabrics they may see."""
    groups: list[CatalogGroup] = []
    app_keys = [product for product, _title in catalog_products()]
    app_rows = (
        await db.execute(
            select(Release).where(
                Release.kind == ReleaseKind.APP.value,
                Release.product.in_(app_keys),
                Release.community_release_id.is_not(None),
            )
        )
    ).scalars()
    by_app: dict[str, list[Release]] = {product: [] for product in app_keys}
    for release in app_rows:
        by_app[release.product].append(release)
    for product, title in catalog_products():
        rows = _newest(by_app[product])
        if rows:
            groups.append(CatalogGroup(ReleaseKind.APP.value, title, [product], rows, rows))

    lines = await _line_titles(db, await installed_products(db, fabric_ids))
    if lines:
        line_rows = (
            await db.execute(
                select(Release).where(
                    Release.kind.in_([ReleaseKind.OS.value, ReleaseKind.FIRMWARE.value]),
                    Release.title.in_([title for _kind, title in lines]),
                    Release.community_release_id.is_not(None),
                )
            )
        ).scalars()
        by_line: dict[tuple[str, str], list[Release]] = {key: [] for key in lines}
        for release in line_rows:
            key = (release.kind, release.title or "")
            if key in by_line and release.product in lines[key]:
                by_line[key].append(release)
        for kind in (ReleaseKind.OS.value, ReleaseKind.FIRMWARE.value):
            for (line_kind, title), products in sorted(lines.items()):
                if line_kind != kind or not by_line[(line_kind, title)]:
                    continue
                newest = _newest(by_line[(line_kind, title)])
                versions = {r.version for r in newest}
                groups.append(
                    CatalogGroup(
                        kind,
                        title,
                        products,
                        newest,
                        [r for r in by_line[(line_kind, title)] if r.version in versions],
                    )
                )
    return groups


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
    # Models on one firmware/OS line share a community post: one assessment
    # per post, copied to the rest when it lands (`share_rating`).
    posts: set[tuple[str, str]] = set()
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
        post = (release.kind, release.community_release_id or "")
        if release.community_release_id and post in posts:
            continue
        posts.add(post)
        claimed.append(str(release.id))
    return claimed


RATING_FIELDS = (
    "risk_level",
    "base_risk_level",
    "reason",
    "summary",
    "issues",
    "sources",
    "assessed_at",
    "assessed_by_model",
    "assessment_error",
)


async def share_rating(db: AsyncSession, release: Release) -> list[Release]:
    """Copy `release`'s fresh AI rating to the other models' rows for the
    same community post (a switch firmware is one post for every switch
    on the line). Human decisions stay per row. Does not commit; returns
    the rows updated."""
    if not release.community_release_id or release.assessed_at is None:
        return []
    siblings = list(
        (
            await db.execute(
                select(Release).where(
                    Release.kind == release.kind,
                    Release.community_release_id == release.community_release_id,
                    Release.id != release.id,
                )
            )
        ).scalars()
    )
    for sibling in siblings:
        for name in RATING_FIELDS:
            setattr(sibling, name, getattr(release, name))
        sibling.assessment_queued_at = None
    return siblings


REQUEUE_AFTER = timedelta(hours=1)


async def visible_verdicts(
    db: AsyncSession, user: User, *, installed: bool = False
) -> list[tuple[Fabric, TargetVerdict]]:
    """Every pending update in the Fabrics `user` may see
    (`app.services.access_scope`), with its verdict — shared by the
    Releases/Assessments pages and the AI assistant's tools. `installed`
    adds what runs now (`TargetSource.INSTALLED`), judged the same way."""
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
        if installed:
            targets += installed_targets(consoles)
        releases = await load_releases(db, targets)
        max_level = max_risk_for(fabric, app_settings.max_acceptable_risk_level)
        out.extend((fabric, v) for v in verdicts(targets, releases, max_level))
    return out
