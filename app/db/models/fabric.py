"""A Fabric: one UniFi Site Manager API key and everything synced under it.

The app's top-level unit of ownership — the per-user scope
(`app.db.models.user_fabric_access`) is granted per Fabric, and a scheduled
task can target a whole Fabric. The inventory below it (consoles, their
applications, sites, devices) is never edited here, only mirrored from the
Site Manager API by the sync job (`app.tasks.jobs.sync_fabric`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Fabric(Base):
    __tablename__ = "fabrics"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The Site Manager API key (`X-API-Key`), via `app.core.security.
    # encrypt_secret` — never stored or logged in plaintext, never rendered
    # back into a form (the edit form only offers "replace").
    api_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # A disabled Fabric is skipped by the periodic sync and by every
    # scheduled task, but keeps its inventory and history.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_sync_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # The last sync's error message, cleared on the next successful sync.
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    consoles: Mapped[list[Console]] = relationship(
        back_populates="fabric", cascade="all, delete-orphan"
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Fabric(id={self.id!r}, name={self.name!r})"


# Imported last, and only for type checking: every class above is already
# defined by the time this module points back at the models it relates
# to, so no import cycle can leave a class half-defined (CodeQL's
# "Module-level cyclic import"). SQLAlchemy resolves the relationship
# targets by name through its registry, never through these imports.
if TYPE_CHECKING:
    from app.db.models.console import Console
