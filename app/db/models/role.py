"""Roles and permissions — the "complete RBAC" layer: an admin defines named
roles (e.g. "Operator", "Auditor") and picks exactly which permissions each
one grants, then assigns one role to each user (see `app.db.models.user`).

Permissions are deliberately resource-grained, not per-object — there's no
"can manage Fabric X but not Fabric Y" here (that is the separate per-user
Fabric scope, `app.services.access_scope`), only "can manage Fabrics at
all". `MANAGE` implies `VIEW` for the same resource (see `_MANAGE_IMPLIES_VIEW`
and `User.has_permission` in `app.db.models.user`) so a role granted e.g.
`FABRIC_MANAGE` doesn't also need `FABRIC_VIEW` ticked separately — that
would be a footgun (a role that can edit Fabrics but, by omission, can't
even load the page listing them).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.pg_enum import pg_enum

if TYPE_CHECKING:
    from app.db.models.user import User


class Permission(enum.StrEnum):
    # Read-only view of Fabrics and everything synced under them (consoles,
    # sites, devices, versions, AI release assessments).
    FABRIC_VIEW = "fabric.view"
    # Add/edit/delete a Fabric — this is where a Site Manager API key is
    # entered, so it is its own trust level, apart from running updates.
    FABRIC_MANAGE = "fabric.manage"
    # Trigger a firmware/app update (manually or by creating a scheduled
    # update task) — only ever for versions whose AI assessment is within
    # the configured acceptable level.
    UPDATE_RUN = "update.run"
    # Allow an update that the AI-assessment policy blocked. Deliberately
    # separate from UPDATE_RUN: overriding the safety gate is a materially
    # bigger decision than running an update the policy already accepts.
    UPDATE_OVERRIDE = "update.override"
    # Re-run an AI release assessment, set a manual score, and edit the
    # acceptable-level policy (global / Fabric / site / device type).
    RELEASE_MANAGE = "release.manage"
    # The AI assistant page (`/ai`). Gates reaching the feature at all and
    # nothing else — every tool the assistant can invoke is separately gated
    # by the same permission a human clicking the equivalent button needs.
    AI_ACCESS = "ai.access"
    SCHEDULING_VIEW = "scheduling.view"
    SCHEDULING_MANAGE = "scheduling.manage"
    # Notification rules, recipient user groups, and email templates
    # (`/notifications`, app/web/routes/notifications.py) — kept apart from
    # SETTINGS_MANAGE since "who gets emailed about what" is a different,
    # narrower trust level than the rest of Settings (SMTP relay, LDAP/OIDC,
    # syslog), and apart from USER_MANAGE since a user group here is a
    # notification-recipient list, not an account-administration concept.
    NOTIFICATION_VIEW = "notification.view"
    NOTIFICATION_MANAGE = "notification.manage"
    AUDIT_VIEW = "audit.view"
    SETTINGS_VIEW = "settings.view"
    SETTINGS_MANAGE = "settings.manage"
    # Users, roles, and sessions ("log out everywhere") are bundled under one
    # permission rather than split further — none of it is meaningful
    # without the others (a role editor who can't also assign roles to users
    # isn't useful on its own).
    USER_MANAGE = "user.manage"
    # Log in as any other account without knowing their password — "Sign in
    # as" from the user list (app/web/routes/impersonation.py). Deliberately
    # its own permission, not implied by USER_MANAGE: editing accounts and
    # roles is one trust level, being able to silently act *as* one of them
    # is a materially bigger one (everything the impersonated session does is
    # audit-logged under both identities — see that module's docstring).
    USER_IMPERSONATE = "user.impersonate"


class Role(Base):
    """A named, reusable set of permissions."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # When true, every user holding this role must have `User.totp_enabled`
    # to do anything except enroll TOTP or log out — enforced live, on every
    # request, in `app.auth.middleware`, not
    # just steered at login time like `User.must_change_password` is. See
    # those modules for the enforcement and wiki/Architecture.md for the
    # OIDC-exemption reasoning (OIDC accounts can't enroll TOTP here at all —
    # a role with this set would otherwise lock them out unconditionally).
    require_totp: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # lazy="selectin": always loaded (one small extra batched query) whenever
    # a Role is, rather than lazy — with the async ORM, a genuinely lazy
    # load here would raise MissingGreenlet the moment anything (like
    # `permissions`/`User.has_permission`) reads it outside of an explicit
    # `selectinload(...)` the caller remembered to add. Roles are few and
    # rarely reloaded, so paying for this eagerly, always, is cheap
    # insurance against that whole class of bug.
    permission_grants: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )
    # ON DELETE RESTRICT (the default for a FK without ondelete=) on
    # User.role_id means the DB itself refuses to delete a role that's still
    # assigned to anyone — belt-and-braces alongside the application-level
    # check in app/web/routes/roles.py.
    users: Mapped[list[User]] = relationship(back_populates="role")

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset(grant.permission for grant in self.permission_grants)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Role(id={self.id!r}, name={self.name!r})"


class RolePermission(Base):
    """One granted permission for one role — a plain association row, not a
    catalog table, since `Permission` is a fixed, code-defined set rather
    than data an admin creates."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[Permission] = mapped_column(
        pg_enum(Permission, name="permission"), primary_key=True
    )

    role: Mapped[Role] = relationship(back_populates="permission_grants")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"RolePermission(role_id={self.role_id!r}, permission={self.permission!r})"
