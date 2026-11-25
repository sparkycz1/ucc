"""One UniFi release ucc has been asked to judge — a UniFi OS version, an
application version (Network, Protect, ...) or a device firmware version —
and its AI risk assessment.

A row is created by `app.releases.discovery` the first time any synced
console or device has an update to that version pending, and assessed by
`app.tasks.release_jobs.assess_release`. Releases are global, not per
Fabric: the same Network 10.0.160 is the same release whichever Fabric runs
it, so one assessment serves all of them. What is per Fabric is the
acceptable level (`AppSettings.max_acceptable_risk_level`, overridable by
`Fabric.max_acceptable_risk_level`) — see `app.releases.verdicts`.

ucc never runs an update itself (Site Manager has no endpoint for it); the
assessment only produces a recommendation — GO or STOP — per pending
update.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MIN_RISK_LEVEL = 1
MAX_RISK_LEVEL = 5


class ReleaseKind(enum.StrEnum):
    """What a release updates. Stored as a plain string (not a Postgres
    enum), so adding a kind later needs no migration of the type."""

    OS = "os"
    APP = "app"
    FIRMWARE = "firmware"


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("kind", "product", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # `unifi-os` for kind=os, the application key (`network`, `protect`,
    # ...) for kind=app, the device model (`U6-Pro`, `USW-24-PoE`, ...) for
    # kind=firmware — firmware versions are per model family, so two models
    # sharing a version number are still two releases.
    product: Mapped[str] = mapped_column(String(100), nullable=False)
    # Normalized (`app.releases.versions.normalize_version`): a camera's
    # `UVC.SAV539g.v5.5.51.abcdef` is stored as `5.5.51`.
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    # A human label for lists, e.g. "UniFi Network Application" or
    # "UniFi OS" — the community release title once one is matched.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The matched community.ui.com release, when there is one.
    community_release_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    community_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # GA, RC, EA, ... as community.ui.com labels it.
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- The AI assessment (overwritten on every re-assessment) ---
    risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The level for a setup none of the hardware-specific issues hit
    # (`app.releases.context`); NULL = the same for everyone.
    base_risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A few words on why — "STP broken, cameras flicker" — for lists.
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"title", "detail", "reports", "severity", "affects": [...]}] — the
    # concrete problems found (older ratings have only title and detail).
    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # [{"kind": "community"|"reddit"|"web", "title": ..., "url": ...}] —
    # what the assessment was based on, shown as links.
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    assessed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # "<provider kind>/<model id>" that produced the assessment.
    assessed_by_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # When an assessment was last enqueued — so several Fabrics discovering
    # the same new release enqueue it once (`app.tasks.release_jobs`).
    assessment_queued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # The last attempt's error, cleared on the next successful assessment.
    assessment_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Human decisions (release.manage / update.override) ---
    # A manual risk level replaces the AI's for every verdict.
    manual_risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manual_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Allowed despite its risk level: every pending update to it is GO.
    allowed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    allowed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    allowed_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def effective_risk_level(self) -> int | None:
        """The level verdicts use: a manual one wins over the AI's."""
        if self.manual_risk_level is not None:
            return self.manual_risk_level
        return self.risk_level

    @property
    def short_reason(self) -> str | None:
        """`reason`, or — for ratings made before it existed — the titles
        of the first issues."""
        if self.reason:
            return self.reason
        titles = [
            str(issue.get("title"))
            for issue in (self.issues or [])[:3]
            if isinstance(issue, dict) and issue.get("title")
        ]
        return ", ".join(titles) or None

    @property
    def is_allowed(self) -> bool:
        return self.allowed_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Release(kind={self.kind!r}, product={self.product!r}, version={self.version!r})"


class LatestRelease(Base):
    """The newest generally-available community.ui.com release for one
    product line, refreshed by `app.tasks.release_jobs.refresh_latest_releases`.

    Shown next to an installed version when Site Manager offers no update
    for it (staged roll-outs, a release channel, or simply nothing known to
    the console yet) — so the Fabric view says "11.0.104 is out" even while
    the console itself still says it is up to date. `current_version` is
    the installed version the line was found from (firmware lines are
    identified by the post for the installed version)."""

    __tablename__ = "latest_releases"
    __table_args__ = (UniqueConstraint("kind", "product", "current_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    product: Mapped[str] = mapped_column(String(100), nullable=False)
    current_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL = looked, and community.ui.com has no matching line.
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(nullable=False)


class RatingSource(enum.StrEnum):
    AI = "ai"
    MANUAL = "manual"
    MANUAL_CLEARED = "manual_cleared"
    ALLOWED = "allowed"
    ALLOW_REVOKED = "allow_revoked"


class ReleaseRating(Base):
    """One entry in a release's rating history: every AI (re-)assessment
    and every human decision on top of it, oldest first on the release's
    page — "2 a week ago, 4 since people started reporting STP loops"."""

    __tablename__ = "release_ratings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # The level this entry set (NULL for a cleared manual level / revoked
    # allowance), and the general level in effect right after it.
    risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_risk_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The model ("anthropic/claude-...") or the person's username.
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )
