from __future__ import annotations

import re

from sqlalchemy import select

from app.db.models.role import Permission, Role
from app.db.models.user import User
from tests.conftest import ADMIN_USERNAME, create_fabric_via_web


def _extract_user_edit_link(users_page_html: str) -> str:
    """Only ever called when exactly one user (the one seeded by the
    `client` fixture) exists yet, so "the only edit link on the page" and
    "that user's edit link" are the same thing."""
    match = re.search(r"/users/([0-9a-f-]{36})/edit", users_page_html)
    assert match is not None, users_page_html
    return match.group(1)


async def test_no_permissions_gets_403_on_a_view_page(client, login_as):
    await login_as(client, permissions=frozenset())
    response = await client.get("/fabrics")
    assert response.status_code == 403


async def test_view_only_permission_allows_get_but_not_post(client, login_as):
    await login_as(client, permissions={Permission.FABRIC_VIEW})
    assert (await client.get("/fabrics")).status_code == 200

    assert (await client.get("/fabrics/new")).status_code == 403
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/fabrics", data={"name": "nope", "api_key": "k", "csrf_token": csrf_token}
    )
    assert response.status_code == 403


async def test_manage_permission_implies_view(client, login_as):
    """Granting FABRIC_MANAGE alone must still allow reaching the (view-gated)
    Fabric list — see app/db/models/user.py's `_MANAGE_IMPLIES_VIEW`."""
    await login_as(client, permissions={Permission.FABRIC_MANAGE})
    assert (await client.get("/fabrics")).status_code == 200


async def test_fabric_scope_hides_other_fabrics(client, login_as, accept_any_api_key):
    """A restricted account sees only its own Fabrics; another one is a
    404, never a 403 (a 403 would confirm it exists)."""
    own_id = await create_fabric_via_web(client, "mine")
    other_id = await create_fabric_via_web(client, "theirs")

    await login_as(client, permissions={Permission.FABRIC_VIEW}, fabric_ids={own_id})
    listing = await client.get("/fabrics")
    assert "mine" in listing.text
    assert "theirs" not in listing.text
    assert (await client.get(f"/fabrics/{own_id}")).status_code == 200
    assert (await client.get(f"/fabrics/{other_id}")).status_code == 404


async def test_user_manage_gates_the_users_and_roles_pages(client, login_as):
    await login_as(client, permissions=frozenset())
    assert (await client.get("/users")).status_code == 403
    assert (await client.get("/roles")).status_code == 403

    await login_as(client, permissions={Permission.USER_MANAGE}, username="user-admin")
    assert (await client.get("/users")).status_code == 200
    assert (await client.get("/roles")).status_code == 200


async def test_nav_only_shows_permitted_links(client, login_as):
    await login_as(client, permissions={Permission.AUDIT_VIEW})
    page = await client.get("/audit")
    assert page.status_code == 200
    assert 'href="/audit"' in page.text
    assert 'href="/fabrics"' not in page.text
    assert '<a href="/users">Users</a>' not in page.text


async def test_cannot_delete_own_account(client):
    users_page = await client.get("/users")
    self_id = _extract_user_edit_link(users_page.text)

    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(f"/users/{self_id}/delete", data={"csrf_token": csrf_token})
    assert response.status_code == 403


async def test_cannot_deactivate_own_account(client, db_session_factory):
    users_page = await client.get("/users")
    self_id = _extract_user_edit_link(users_page.text)

    async with db_session_factory() as db:
        result = await db.execute(select(User).where(User.username == ADMIN_USERNAME))
        own_role_id = result.scalar_one().role_id

    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        f"/users/{self_id}/edit",
        data={
            "username": ADMIN_USERNAME,
            "display_name": "",
            "auth_provider": "local",
            "password": "",
            "role_id": str(own_role_id),
            # is_active omitted — an unchecked checkbox.
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 403
    assert "own account" in response.text


async def test_removing_user_manage_from_the_only_admin_role_is_blocked(client, db_session_factory):
    """The acting admin's own role is the only one granting `user.manage` —
    editing it to drop that permission would leave nobody able to manage
    users, including the person making the change."""
    async with db_session_factory() as db:
        result = await db.execute(select(Role))
        admin_role = result.scalars().one()  # the `client` fixture creates exactly one role

    edit_page = await client.get(f"/roles/{admin_role.id}/edit")
    assert edit_page.status_code == 200

    csrf_token = client.cookies.get("csrftoken")
    remaining_permissions = [p.value for p in Permission if p != Permission.USER_MANAGE]
    response = await client.post(
        f"/roles/{admin_role.id}/edit",
        data={
            "name": admin_role.name,
            "description": "",
            "permissions": remaining_permissions,
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 409
    assert "no other active account" in response.text
