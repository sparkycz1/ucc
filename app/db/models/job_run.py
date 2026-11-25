"""The last run of each periodic background job, for the Status page.

One row per job name (a Celery Beat entry such as `refresh-release-catalog`),
overwritten on every run by `app.tasks.job_runs.run_tracked` — a status
board, not a history: the audit log and the worker's own log keep the
history. The last failure is kept apart from the last run, so a job that
failed at night and recovered since still shows when and why.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # NULL while the first run is still going.
    last_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # A short summary of the last result ("20 releases, 3 queued").
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
