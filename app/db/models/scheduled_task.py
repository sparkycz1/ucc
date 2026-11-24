"""A user-defined, cron-scheduled action against a site, a Fabric, or
"All Fabrics" — see `app.scheduling` for the action registry and the
background jobs that evaluate and run these.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.fabric import Fabric
from app.db.models.site import Site
from app.db.pg_enum import pg_enum


class ScheduleTargetType(enum.StrEnum):
    SITE = "site"
    FABRIC = "fabric"
    ALL_FABRICS = "all_fabrics"


class ScheduledTask(Base):
    """One recurring action: run `action` (a key into the schedulable-action
    registry, `app.scheduling.actions`) against `target_type`'s Fabrics/sites,
    on the schedule described by `cron_expression` (standard 5-field cron,
    interpreted in UTC).

    Adding a brand new kind of action to the app doesn't require any change
    here — it only needs a `ScheduledActionSpec` registered in
    `app.scheduling.builtin_actions`, and it becomes selectable in the
    "New scheduled task" form automatically.
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Key into app.scheduling.actions' registry (e.g. "sync_inventory") —
    # deliberately a plain string, not a native enum, so a new action can be
    # registered without a migration.
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    # Per-action options.
    # Shape is defined by that action's `ScheduledActionParam`s, not enforced
    # by the DB.
    action_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    target_type: Mapped[ScheduleTargetType] = mapped_column(
        pg_enum(ScheduleTargetType, name="schedule_target_type"), nullable=False
    )
    # Exactly one of these is set, matching target_type — enforced in the
    # route/schema layer, not via a DB constraint (SQLite in tests doesn't
    # make a CHECK constraint across nullable FKs pleasant, and there's only
    # ever one writer: the scheduling form).
    target_fabric_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fabrics.id", ondelete="CASCADE"), nullable=True
    )
    target_fabric: Mapped[Fabric | None] = relationship()
    target_site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=True
    )
    target_site: Mapped[Site | None] = relationship()

    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Denormalized so the per-minute tick (`app.scheduling.jobs.run_due_scheduled_tasks`)
    # is a single indexed `WHERE next_run_at <= now` query instead of every
    # tick re-parsing every enabled task's cron expression. Recomputed on
    # create/edit and after every run.
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Short human-readable outcome of the most recent run (e.g. "Triggered
    # for 3 target(s), 1 skipped."). Per-run history is `ScheduledTaskRun`.
    last_run_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"ScheduledTask(id={self.id!r}, name={self.name!r}, "
            f"action={self.action!r}, cron={self.cron_expression!r})"
        )
