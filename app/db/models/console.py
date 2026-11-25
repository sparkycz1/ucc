"""A UniFi console (Site Manager "host": UDM, UCG, UNVR, Cloud Key, ...) and
the UniFi OS applications installed on it (Network, Protect, ...).

Mirrored from the Site Manager API by `app.tasks.jobs.sync_fabric` — every
column except the primary key is overwritten on each sync.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Console(Base):
    __tablename__ = "consoles"
    __table_args__ = (UniqueConstraint("fabric_id", "host_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    fabric_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fabrics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fabric: Mapped[Fabric] = relationship(back_populates="consoles")

    # Site Manager's host id — also the `{id}` of the connector proxy path.
    host_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Site Manager's model code (`hardware.shortname`, "UDRULT") — what
    # releases are keyed on — and the name people know it by ("UCG-Ultra"),
    # display only, NULL until the next sync.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # UniFi OS (`hardware.firmwareVersion`) and the update Site Manager
    # offers for it.
    os_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os_update_available: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # `reportedState.version` — a platform build number, not the version that
    # updates (that is `os_version`); secondary information only.
    core_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # UniFi's own automatic updates, as the console reports them. These run
    # on UniFi's side and bypass ucc's AI gate, so the UI warns about them;
    # ucc can't change them (UniFi OS endpoints are 403 through the
    # connector proxy). `None` = the console reports no schedule.
    auto_update_schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auto_update_include_apps: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)

    apps: Mapped[list[ConsoleApp]] = relationship(
        back_populates="console", cascade="all, delete-orphan", lazy="selectin"
    )
    sites: Mapped[list[Site]] = relationship(back_populates="console", cascade="all, delete-orphan")
    devices: Mapped[list[Device]] = relationship(
        back_populates="console", cascade="all, delete-orphan"
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Console(id={self.id!r}, host_id={self.host_id!r})"


class ConsoleApp(Base):
    """One UniFi OS application on a console, e.g. `network` 9.4.19."""

    __tablename__ = "console_apps"
    __table_args__ = (UniqueConstraint("console_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    console_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("consoles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    console: Mapped[Console] = relationship(back_populates="apps")

    # Site Manager's application key: network, protect, access, talk, ...
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    update_available: Mapped[str | None] = mapped_column(String(64), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


# Imported last, and only for type checking: every class above is already
# defined by the time this module points back at the models it relates
# to, so no import cycle can leave a class half-defined (CodeQL's
# "Module-level cyclic import"). SQLAlchemy resolves the relationship
# targets by name through its registry, never through these imports.
if TYPE_CHECKING:
    from app.db.models.device import Device
    from app.db.models.fabric import Fabric
    from app.db.models.site import Site
