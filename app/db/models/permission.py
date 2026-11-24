"""Every permission a role can grant — see `app.db.models.role` for how roles
bundle them and `app.db.models.user` for how MANAGE implies VIEW.

Its own module, with no model imports, so that `role`, `user` and
`temporary_permission_grant` can all use it at class-definition time
without importing each other (no import cycle between the models).
"""

import enum


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
