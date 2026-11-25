"""A UniFi device (AP, switch, gateway, camera, ...) adopted on a console,
mirrored from the Site Manager API.

Site Manager's `/v1/devices` groups devices by *console*, not by site, so
`console_id` is the required parent and `site_id` is filled in only where
the site is known (single-site consoles, or a later per-site lookup through
the connector proxy)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ColumnElement, ForeignKey, String, UniqueConstraint, func, or_
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Site Manager's `firmwareStatus` value for "a newer firmware exists".
UPDATE_AVAILABLE_STATUS = "updateAvailable"


def device_has_update() -> ColumnElement[bool]:
    """SQL twin of `Device.has_update` — for counting/filtering in queries."""
    return or_(
        Device.update_available.is_not(None),
        Device.firmware_status == UPDATE_AVAILABLE_STATUS,
    )


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("console_id", "mac"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    console_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("consoles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    console: Mapped[Console] = relationship(back_populates="devices")
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    site: Mapped[Site | None] = relationship(back_populates="devices")

    mac: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Short model code, e.g. "U6-Pro", "USW-24-PoE" — what release notes and
    # AI assessments are keyed on together with the version.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # The name people know it by ("UCG-Ultra", "USW-Pro-Max-48-PoE") —
    # Site Manager's `model`, while `model` above holds its `shortname`
    # code ("UDRULT", "USPM48P"). Display only; NULL until the next sync.
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # network, protect, access, ...
    product_line: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    update_available: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Site Manager's own verdict ("upToDate", "updateAvailable", ...) — it can
    # say an update exists while `update_available` is empty (offline devices).
    firmware_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @property
    def has_update(self) -> bool:
        return bool(self.update_available) or self.firmware_status == UPDATE_AVAILABLE_STATUS

    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Device(id={self.id!r}, mac={self.mac!r}, model={self.model!r})"


# Imported last, and only for type checking: every class above is already
# defined by the time this module points back at the models it relates
# to, so no import cycle can leave a class half-defined (CodeQL's
# "Module-level cyclic import"). SQLAlchemy resolves the relationship
# targets by name through its registry, never through these imports.
if TYPE_CHECKING:
    from app.db.models.console import Console
    from app.db.models.site import Site
