"""Notification rules — "when X happens, tell these people, about these
Fabrics" — and the (small, fixed, code-defined) set of events that can
trigger one. See `app.services.notifications` for the dispatch/send logic
that actually reads these; this module only holds the data.

**Recipients** are the union of a rule's directly-listed `users` and every
active user whose `Role` is one of the rule's `roles` — deliberately
targeting the existing RBAC `Role` rather than a separate notification-only
grouping concept (an earlier round of this feature had its own `UserGroup`
model; it was folded into `Role` once it became clear "who should hear
about what" almost always tracks "what job does this account do", which a
role already answers, and a second, parallel grouping concept just for
notifications was one more list to keep in sync as accounts come and go).
A user with no `User.email` set is silently skipped, not an error, the same
"missing config = no-op" spirit `AppSettings.smtp_enabled` already has.

**Scope** narrows *which Fabrics* a rule cares about: an empty `fabrics`
list means "every Fabric". An event with no Fabric at all matches every
scope, since there's nothing to check it against.

**Delivery** is email (through the SMTP relay configured in Settings →
Integrations, `AppSettings.smtp_*`) or a webhook (`webhook_url`, plain
JSON POST) — one or the other per rule, `delivery_channel` says which.
For email, a rule with no matching recipients, or an SMTP relay that
isn't enabled, is a silent no-op; a webhook rule with SMTP disabled still
fires (the two channels don't depend on each other). Either way, a
delivery attempt is never able to break whatever background job the
triggering event happened during — see `app.services.notifications.notify`
— and every attempt (success or failure) is recorded in
`app.db.models.notification_log.NotificationLog` for troubleshooting.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Column, ForeignKey, String, Table, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class NotificationEventType(enum.StrEnum):
    """The fixed set of events a rule can fire on. Adding a new one means
    adding a member here, wiring exactly one `notify(...)` call at the
    point the event actually happens (see `app.services.notifications`'s
    module docstring for the current call sites), and adding its default
    subject/body in `_DEFAULT_TEMPLATES` (`app.services.notifications`) —
    the same three-step recipe every existing member below followed."""

    FABRIC_SYNC_FAILED = "fabric.sync_failed"
    # An AI release assessment finished (new version, or a re-assessment).
    RELEASE_ASSESSED = "release.assessed"
    # A (scheduled or manual) update was stopped because the version's AI
    # risk level is above the acceptable level — someone with
    # `update.override` can allow it by hand.
    UPDATE_BLOCKED = "update.blocked"
    UPDATE_RUN_SUCCEEDED = "update.succeeded"
    UPDATE_RUN_FAILED = "update.failed"


notification_rule_users = Table(
    "notification_rule_users",
    Base.metadata,
    Column("rule_id", ForeignKey("notification_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

notification_rule_roles = Table(
    "notification_rule_roles",
    Base.metadata,
    Column("rule_id", ForeignKey("notification_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

notification_rule_fabrics = Table(
    "notification_rule_fabrics",
    Base.metadata,
    Column("rule_id", ForeignKey("notification_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("fabric_id", ForeignKey("fabrics.id", ondelete="CASCADE"), primary_key=True),
)


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # A JSON array of `NotificationEventType` values — same "small
    # code-defined set, stored as JSON rather than a join table" choice as
    # the role permissions. Validated against the enum wherever a
    # rule is created/edited (`app/web/routes/notifications.py`), never
    # trusted as-is from the database.
    event_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users: Mapped[list[User]] = relationship(secondary=notification_rule_users, lazy="selectin")
    roles: Mapped[list[Role]] = relationship(secondary=notification_rule_roles, lazy="selectin")
    fabrics: Mapped[list[Fabric]] = relationship(
        secondary=notification_rule_fabrics, lazy="selectin"
    )

    custom_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notification_custom_templates.id", ondelete="SET NULL"), nullable=True
    )
    custom_template: Mapped[NotificationCustomTemplate | None] = relationship(lazy="selectin")

    # "email" (default) or "webhook" — see the module docstring's Delivery
    # section. Plain string, not a native DB enum, same "small code-defined
    # set" reasoning as `event_types` above: adding a third channel later
    # needs no migration to widen a DB-level enum type.
    delivery_channel: Mapped[str] = mapped_column(String(16), default="email", nullable=False)
    # Required (validated in app/web/routes/notifications.py) when
    # delivery_channel is "webhook"; unused/ignored for "email".
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    @property
    def event_type_enums(self) -> list[NotificationEventType]:
        """`event_types`, parsed — silently drops any value that isn't a
        known member instead of raising, so a future removal of an event
        type doesn't turn every *other* rule's page into a 500."""
        result = []
        for raw in self.event_types:
            try:
                result.append(NotificationEventType(raw))
            except ValueError:
                continue
        return result

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"NotificationRule(id={self.id!r}, name={self.name!r})"


class NotificationTemplate(Base):
    """An admin-editable override of the built-in subject/body for one
    `NotificationEventType`. An event type with no row here just uses the
    built-in default (`app.services.notifications._DEFAULT_TEMPLATES`) —
    there's no migration/seed step needed to "install" the defaults, and
    deleting a customized template row is exactly "reset to default."""

    __tablename__ = "notification_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    # Plain text, not HTML — rendered with a small `{placeholder}`
    # substitution (`app.services.notifications.render_template`), not a
    # template engine, so an admin-edited body can never execute code or
    # reach outside its own string. See that function's docstring for the
    # available placeholders.
    body: Mapped[str] = mapped_column(String(4000), nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"NotificationTemplate(event_type={self.event_type!r})"


class NotificationCustomTemplate(Base):
    """A named, reusable subject/body an admin writes once and any number of
    `NotificationRule`s can opt into via `NotificationRule.custom_template_id`
    — distinct from `NotificationTemplate` above, which is a single
    per-event-type override applied to *every* rule that fires that event.
    A rule with no `custom_template_id` keeps using that per-event
    default/override exactly as before ("missing config = no change" — same
    spirit as everywhere else in this module); setting one overrides it for
    that rule alone, regardless of which event actually fired. Same plain-
    text `{placeholder}` substitution as `NotificationTemplate`
    (`app.services.notifications.render_template`) — every placeholder
    listed there works here too. Deleting a template in use just clears the
    referencing rule(s) back to their per-event default (`ondelete="SET
    NULL"` on the FK), never breaks them."""

    __tablename__ = "notification_custom_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"NotificationCustomTemplate(name={self.name!r})"


# Imported last, and only for type checking: every class above is already
# defined by the time this module points back at the models it relates
# to, so no import cycle can leave a class half-defined (CodeQL's
# "Module-level cyclic import"). SQLAlchemy resolves the relationship
# targets by name through its registry, never through these imports.
if TYPE_CHECKING:
    from app.db.models.fabric import Fabric
    from app.db.models.role import Role
    from app.db.models.user import User
