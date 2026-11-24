"""Time-limited per-user permissions — app.services.temporary_permissions,
User.has_permission's live expiry check, and the Users page's grant/revoke
UI.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.models.role import Permission
from app.db.models.temporary_permission_grant import TemporaryPermissionGrant
from app.db.models.user import User


async def _grant(
    db_session_factory: Any,
    user_id: uuid.UUID,
    *,
    permission: Permission,
    hours_from_now: float = 1.0,
    revoked: bool = False,
) -> None:
    async with db_session_factory() as db:
        now = datetime.now(UTC)
        db.add(
            TemporaryPermissionGrant(
                user_id=user_id,
                permission=permission,
                granted_by_id=None,
                granted_at=now,
                expires_at=now + timedelta(hours=hours_from_now),
                revoked_at=now if revoked else None,
            )
        )
        await db.commit()


async def test_has_permission_true_with_active_temporary_grant(
    client, db_session_factory, login_as
):
    user = await login_as(client, permissions=frozenset())
    await _grant(db_session_factory, user.id, permission=Permission.AUDIT_VIEW, hours_from_now=1)

    async with db_session_factory() as db:
        reloaded = await db.get(User, user.id)
        assert reloaded is not None
        assert reloaded.has_permission(Permission.AUDIT_VIEW) is True


async def test_has_permission_false_once_expired(client, db_session_factory, login_as):
    user = await login_as(client, permissions=frozenset())
    await _grant(db_session_factory, user.id, permission=Permission.AUDIT_VIEW, hours_from_now=-1)

    async with db_session_factory() as db:
        reloaded = await db.get(User, user.id)
        assert reloaded is not None
        assert reloaded.has_permission(Permission.AUDIT_VIEW) is False


async def test_has_permission_false_once_revoked(client, db_session_factory, login_as):
    user = await login_as(client, permissions=frozenset())
    await _grant(
        db_session_factory,
        user.id,
        permission=Permission.AUDIT_VIEW,
        hours_from_now=1,
        revoked=True,
    )

    async with db_session_factory() as db:
        reloaded = await db.get(User, user.id)
        assert reloaded is not None
        assert reloaded.has_permission(Permission.AUDIT_VIEW) is False


async def test_temporary_grant_unlocks_a_gated_page_end_to_end(
    client, db_session_factory, login_as
):
    user = await login_as(client, permissions=frozenset())

    denied = await client.get("/audit")
    assert denied.status_code == 403

    await _grant(db_session_factory, user.id, permission=Permission.AUDIT_VIEW, hours_from_now=1)

    allowed = await client.get("/audit")
    assert allowed.status_code == 200


async def test_grant_temporary_permission_via_web_form(client, db_session_factory, login_as):
    target = await login_as(client, permissions=frozenset(), username="temp-target")
    # `login_as` replaced the admin session with the restricted target's —
    # switch back to a real user.manage account to grant something.
    from app.db.models.role import Permission as P

    await login_as(client, permissions=set(P), username="admin-granter")

    await client.get(f"/users/{target.id}/edit")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        f"/users/{target.id}/temporary-permissions",
        data={"csrf_token": csrf_token, "permission": "audit.view", "hours": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db_session_factory() as db:
        reloaded = await db.get(User, target.id)
        assert reloaded is not None
        assert reloaded.has_permission(Permission.AUDIT_VIEW) is True
        assert len(reloaded.temporary_permission_grants) == 1


async def test_grant_rejects_out_of_range_hours(client, db_session_factory, login_as):
    from app.db.models.role import Permission as P

    target = await login_as(client, permissions=frozenset(), username="bad-hours-target")
    await login_as(client, permissions=set(P), username="admin-granter-2")

    await client.get(f"/users/{target.id}/edit")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        f"/users/{target.id}/temporary-permissions",
        data={"csrf_token": csrf_token, "permission": "audit.view", "hours": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "perm_error" in response.headers["location"]

    async with db_session_factory() as db:
        reloaded = await db.get(User, target.id)
        assert reloaded is not None
        assert reloaded.temporary_permission_grants == []


async def test_revoke_temporary_permission_early(client, db_session_factory, login_as):
    from app.db.models.role import Permission as P

    target = await login_as(client, permissions=frozenset(), username="revoke-target")
    await login_as(client, permissions=set(P), username="admin-granter-3")

    await _grant(db_session_factory, target.id, permission=Permission.AUDIT_VIEW, hours_from_now=5)
    async with db_session_factory() as db:
        reloaded = await db.get(User, target.id)
        assert reloaded is not None
        grant_id = reloaded.temporary_permission_grants[0].id

    await client.get(f"/users/{target.id}/edit")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        f"/users/{target.id}/temporary-permissions/{grant_id}/revoke",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db_session_factory() as db:
        reloaded = await db.get(User, target.id)
        assert reloaded is not None
        assert reloaded.has_permission(Permission.AUDIT_VIEW) is False
        assert reloaded.temporary_permission_grants[0].revoked_at is not None
