from __future__ import annotations

from tests.conftest import ADMIN_USERNAME, create_fabric_via_web


async def test_audit_log_empty_state(client):
    response = await client.get("/audit")
    assert response.status_code == 200
    assert "No audit log entries" in response.text


async def test_audit_nav_link_present(client):
    response = await client.get("/fabrics")
    assert 'href="/audit"' in response.text


async def test_audit_log_search_filters_entries(client):
    await client.get("/machines/new")
    csrf_token = client.cookies.get("csrftoken")

    await client.post(
        "/machines",
        data={
            "name": "findme",
            "ip_address": "10.9.9.2",
            "port": "22",
            "username": "admin",
            "auth_method": "password",
            "secret": "",
            "csrf_token": csrf_token,
        },
    )
    await client.post(
        "/machines",
        data={
            "name": "other",
            "ip_address": "10.9.9.3",
            "port": "22",
            "username": "admin",
            "auth_method": "password",
            "secret": "",
            "csrf_token": csrf_token,
        },
    )

    filtered = await client.get("/audit?q=findme")
    assert "findme" in filtered.text
    assert "other" not in filtered.text


async def test_scheduled_task_lifecycle_writes_audit_entries(client):
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    await client.post(
        "/scheduling",
        data={
            "name": "audited-schedule",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "0 3 * * *",
            "is_enabled": "on",
            "csrf_token": csrf_token,
        },
    )

    log = await client.get("/audit")
    assert "scheduled_task.create" in log.text
    assert "audited-schedule" in log.text


async def test_successful_login_is_logged(anonymous_client, db_session_factory):
    from app.db.models.role import Permission
    from tests.conftest import create_local_user

    await create_local_user(
        db_session_factory,
        username="audited-user",
        password="a-very-good-password-123",
        permissions={Permission.AUDIT_VIEW},
    )
    await anonymous_client.get("/login")
    csrf_token = anonymous_client.cookies.get("csrftoken")
    await anonymous_client.post(
        "/login",
        data={
            "username": "audited-user",
            "password": "a-very-good-password-123",
            "csrf_token": csrf_token,
        },
    )

    log = await anonymous_client.get("/audit")
    assert log.status_code == 200
    assert "user.login" in log.text
    assert "audited-user" in log.text
    assert "127.0.0.1" in log.text


async def test_failed_login_is_logged_as_denied(anonymous_client, db_session_factory):
    from app.db.models.role import Permission
    from tests.conftest import create_local_user

    # An account that can view the audit log, so it's the one checking it —
    # the *failed* login attempt below is for a different, nonexistent user.
    await create_local_user(
        db_session_factory,
        username="auditor",
        password="a-very-good-password-123",
        permissions={Permission.AUDIT_VIEW},
    )
    await anonymous_client.get("/login")
    csrf_token = anonymous_client.cookies.get("csrftoken")
    await anonymous_client.post(
        "/login", data={"username": "nobody-at-all", "password": "wrong", "csrf_token": csrf_token}
    )

    csrf_token = anonymous_client.cookies.get("csrftoken")
    await anonymous_client.post(
        "/login",
        data={
            "username": "auditor",
            "password": "a-very-good-password-123",
            "csrf_token": csrf_token,
        },
    )

    log = await anonymous_client.get("/audit?outcome=denied")
    assert "user.login" in log.text
    assert "nobody-at-all" in log.text


async def test_creating_a_user_writes_an_audit_entry(client):
    import re

    await client.get("/roles")
    csrf_token = client.cookies.get("csrftoken")
    await client.post(
        "/roles",
        data={
            "name": "Auditable Role",
            "description": "",
            "permissions": [],
            "csrf_token": csrf_token,
        },
    )
    roles_page = await client.get("/roles")
    match = re.search(r"/roles/([0-9a-f-]{36})/edit", roles_page.text)
    assert match is not None
    role_id = match.group(1)

    await client.get("/users/new")
    csrf_token = client.cookies.get("csrftoken")
    await client.post(
        "/users",
        data={
            "username": "audited-new-user",
            "display_name": "",
            "auth_provider": "ldap",
            "password": "",
            "role_id": role_id,
            "csrf_token": csrf_token,
        },
    )

    log = await client.get("/audit")
    assert "user.create" in log.text
    assert "audited-new-user" in log.text
    assert "role.create" in log.text


async def test_creating_a_fabric_writes_audit_entry(client, accept_any_api_key):
    await create_fabric_via_web(client, "audit-me")

    log = await client.get("/audit")
    assert log.status_code == 200
    assert "fabric.create" in log.text
    assert "audit-me" in log.text
    # httpx's ASGITransport reports a fixed client address for test requests.
    assert "127.0.0.1" in log.text
    assert "badge-ok" in log.text  # success outcome
    assert ADMIN_USERNAME in log.text  # the logged-in actor


async def test_the_api_key_never_appears_in_the_audit_log(client, accept_any_api_key):
    await create_fabric_via_web(client, "secret-check", api_key="sm-key-do-not-log-1234")

    log = await client.get("/audit")
    assert "sm-key-do-not-log-1234" not in log.text


async def test_deleting_a_fabric_writes_audit_entry_with_target_label(client, accept_any_api_key):
    fabric_id = await create_fabric_via_web(client, "delete-me")
    csrf_token = client.cookies.get("csrftoken")

    await client.post(f"/fabrics/{fabric_id}/delete", data={"csrf_token": csrf_token})

    log = await client.get("/audit")
    assert "fabric.delete" in log.text
    assert "delete-me" in log.text


async def test_rejected_api_key_is_logged_as_failure(client, monkeypatch):
    from app.web.routes import fabrics as fabrics_routes

    async def _reject(api_key: str) -> str | None:
        return "UniFi Site Manager rejected this API key."

    monkeypatch.setattr(fabrics_routes, "_validate_api_key", _reject)
    await client.get("/fabrics/new")
    response = await client.post(
        "/fabrics",
        data={
            "name": "bad-key",
            "api_key": "nope",
            "csrf_token": client.cookies.get("csrftoken"),
        },
    )
    assert response.status_code == 422
    assert "rejected this API key" in response.text

    log = await client.get("/audit?outcome=failure")
    assert "fabric.create" in log.text
    assert "bad-key" in log.text


async def test_duplicate_fabric_name_is_logged_as_failure(client, accept_any_api_key):
    await create_fabric_via_web(client, "dupe")
    dupe = await client.post(
        "/fabrics",
        data={"name": "dupe", "api_key": "k", "csrf_token": client.cookies.get("csrftoken")},
    )
    assert dupe.status_code == 409

    log = await client.get("/audit?outcome=failure")
    assert "fabric.create" in log.text
    assert "already exists" in log.text


async def test_audit_log_filters_by_exact_fabric_target(client, accept_any_api_key):
    fabric_id = await create_fabric_via_web(client, "target-a")
    await create_fabric_via_web(client, "target-b")

    response = await client.get(f"/audit?target_type=fabric&target_id={fabric_id}")

    assert response.status_code == 200
    assert "target-a" in response.text
    assert "target-b" not in response.text
    assert f'value="{fabric_id}"' in response.text  # hidden target_id carried through the form


async def test_audit_export_respects_target_filter(client, accept_any_api_key):
    fabric_id = await create_fabric_via_web(client, "export-target")
    await create_fabric_via_web(client, "export-other")

    response = await client.get(
        f"/audit/export?format=json&target_type=fabric&target_id={fabric_id}"
    )

    assert response.status_code == 200
    assert "export-target" in response.text
    assert "export-other" not in response.text
