"""A release's rating history (`ReleaseRating`) and the snapshot used to
tell what a re-assessment *changed*.

Every AI (re-)assessment and every human decision appends one entry, so a
release's page shows how its rating moved ("2 a week ago, 4 since people
reported STP loops"). Entries are added to the session by the caller's own
transaction — the history commits with the change it records, or not at
all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.release import RatingSource, Release, ReleaseRating

# Shown on a release's page; older entries are kept but not listed.
HISTORY_SHOWN = 30


@dataclass(frozen=True)
class RatingSnapshot:
    """A release's rating before a change — duck-typed like a `Release` for
    `app.releases.context.contextual_risk`."""

    risk_level: int | None
    base_risk_level: int | None
    manual_risk_level: int | None
    issues: list[dict[str, Any]] | None
    allowed: bool

    @classmethod
    def of(cls, release: Release) -> RatingSnapshot:
        return cls(
            release.risk_level,
            release.base_risk_level,
            release.manual_risk_level,
            list(release.issues or []),
            release.is_allowed,
        )

    @property
    def effective_risk_level(self) -> int | None:
        return self.manual_risk_level if self.manual_risk_level is not None else self.risk_level


def record_rating(
    db: AsyncSession, release: Release, source: RatingSource, actor: str | None
) -> ReleaseRating:
    """Append one history entry for the change just made to `release`."""
    level = {
        RatingSource.AI: release.risk_level,
        RatingSource.MANUAL: release.manual_risk_level,
    }.get(source)
    entry = ReleaseRating(
        release_id=release.id,
        source=source.value,
        risk_level=level,
        effective_risk_level=release.effective_risk_level,
        reason=(
            release.reason
            if source is RatingSource.AI
            else (release.manual_note if source is RatingSource.MANUAL else release.allowed_note)
        ),
        actor=(actor or "")[:255] or None,
    )
    if entry.reason:
        entry.reason = entry.reason[:200]
    db.add(entry)
    return entry


async def rating_history(db: AsyncSession, release: Release) -> list[ReleaseRating]:
    """Newest first, at most `HISTORY_SHOWN`."""
    return list(
        (
            await db.execute(
                select(ReleaseRating)
                .where(ReleaseRating.release_id == release.id)
                .order_by(ReleaseRating.created_at.desc(), ReleaseRating.id)
                .limit(HISTORY_SHOWN)
            )
        ).scalars()
    )
