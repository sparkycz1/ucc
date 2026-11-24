"""The release pieces the jobs and web routes share: which model assesses,
gathering evidence from every configured source, refreshing the newest
community releases, and making sure every pending update has a `Release`
row."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig
from app.db.models.app_settings import AppSettings, WebSearchKind
from app.db.models.release import LatestRelease, Release
from app.releases.evidence import Evidence, SourceError
from app.releases.products import search_term
from app.releases.sources.community import CommunityClient
from app.releases.sources.reddit import RedditClient
from app.releases.sources.web import WebSearchClient
from app.releases.verdicts import UpdateTarget, load_latest, load_releases

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
