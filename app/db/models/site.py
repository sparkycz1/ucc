"""A UniFi Network site on a console, mirrored from the Site Manager API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("console_id", "site_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    console_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("consoles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    console: Mapped[Console] = relationship(back_populates="sites")

    # Site Manager's site id.
    site_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The legacy Network API's short site name (e.g. "default") — what
    # `/proxy/network/api/s/{name}/...` paths take.
    internal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    devices: Mapped[list[Device]] = relationship(back_populates="site")

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Site(id={self.id!r}, name={self.name!r})"


# Imported last, and only for type checking: every class above is already
# defined by the time this module points back at the models it relates
# to, so no import cycle can leave a class half-defined (CodeQL's
# "Module-level cyclic import"). SQLAlchemy resolves the relationship
# targets by name through its registry, never through these imports.
if TYPE_CHECKING:
    from app.db.models.console import Console
    from app.db.models.device import Device
