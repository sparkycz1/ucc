"""The Fabrics pages: add/edit/delete, API key handling, sync trigger, and
the inventory view."""

from __future__ import annotations

from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.models.console import Console, ConsoleApp
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.role import Permission
from tests.conftest import create_fabric_via_web


async def test_creating_a_fabric_validates_and_encrypts_the_key_and_queues_a_sync(
    client, db_session_factory, accept_any_api_key, celery_calls
):
    fabric_id = await create_fabric_via_web(client, "Office", api_key="sm-secret-key")

    assert accept_any_api_key == ["sm-secret-key"]
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        assert fabric.api_key_encrypted != b"sm-secret-key"
        assert decrypt_secret(fabric.api_key_encrypted) == "sm-secret-key"
    assert celery_calls.names == ["app.tasks.jobs.sync_fabric"]


async def test_the_key_is_required_on_create(client, accept_any_api_key):
    await client.get("/fabrics/new")
    response = await client.post(
        "/fabrics", data={"name": "No key", "csrf_token": client.cookies.get("csrftoken")}
    )
    assert response.status_code == 422
    assert "API key is required" in response.text
    assert accept_any_api_key == []


async def test_create_requires_csrf(client, accept_any_api_key):
    response = await client.post(
        "/fabrics", data={"name": "x", "api_key": "k", "csrf_token": "wrong"}
    )
    assert response.status_code == 403


async def test_edit_page_never_renders_the_stored_key(client, accept_any_api_key):
    fabric_id = await create_fabric_via_web(client, "Office", api_key="sm-secret-key")
    page = await client.get(f"/fabrics/{fabric_id}/edit")
    assert page.status_code == 200
    assert "sm-secret-key" not in page.text


async def test_editing_with_a_blank_key_keeps_the_stored_one(
    client, db_session_factory, accept_any_api_key
):
    fabric_id = await create_fabric_via_web(client, "Office", api_key="original")
    response = await client.post(
        f"/fabrics/{fabric_id}/edit",
        data={"name": "Office HQ", "api_key": "", "csrf_token": client.cookies.get("csrftoken")},
    )
    assert response.status_code == 303
    assert accept_any_api_key == ["original"]  # the blank key wasn't validated
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        assert fabric.name == "Office HQ"
        assert fabric.is_enabled is False  # checkbox not submitted
        assert decrypt_secret(fabric.api_key_encrypted) == "original"


async def test_replacing_the_key_validates_it_and_clears_the_sync_error(
    client, db_session_factory, accept_any_api_key, celery_calls
):
    fabric_id = await create_fabric_via_web(client, "Office", api_key="old")
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        fabric.last_sync_error = "HTTP 401"
        await db.commit()

    await client.post(
        f"/fabrics/{fabric_id}/edit",
        data={
            "name": "Office",
            "api_key": "new",
            "is_enabled": "1",
            "csrf_token": client.cookies.get("csrftoken"),
        },
    )
    assert accept_any_api_key == ["old", "new"]
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        assert decrypt_secret(fabric.api_key_encrypted) == "new"
        assert fabric.last_sync_error is None
    assert celery_calls.names.count("app.tasks.jobs.sync_fabric") == 2


async def test_detail_page_shows_synced_versions(client, db_session_factory, accept_any_api_key):
    fabric_id = await create_fabric_via_web(client, "Office")
    async with db_session_factory() as db:
        console = Console(
            fabric_id=fabric_id,
            host_id="h1",
            name="HQ UDM",
            is_online=True,
            os_version="4.1.13",
            os_update_available="4.2.5",
        )
        console.apps = [ConsoleApp(name="network", version="9.0.114", update_available="9.1.120")]
        console.devices = [
            Device(mac="aa:bb:cc:00:00:01", name="AP Lobby", model="U6-Pro", version="6.6.77")
        ]
        db.add(console)
        await db.commit()

    page = await client.get(f"/fabrics/{fabric_id}")
    assert page.status_code == 200
    for expected in ("HQ UDM", "4.1.13", "4.2.5", "9.1.120", "AP Lobby", "U6-Pro", "6.6.77"):
        assert expected in page.text
    assert f'data-live-fabric-id="{fabric_id}"' in page.text

    panel = await client.get(f"/fabrics/{fabric_id}/inventory-panel")
    assert panel.status_code == 200
    assert "AP Lobby" in panel.text


async def test_view_only_user_may_request_a_sync(
    client, login_as, accept_any_api_key, celery_calls
):
    fabric_id = await create_fabric_via_web(client, "Office")
    await login_as(client, permissions={Permission.FABRIC_VIEW}, username="viewer")
    await client.get(f"/fabrics/{fabric_id}")

    response = await client.post(
        f"/fabrics/{fabric_id}/sync", data={"csrf_token": client.cookies.get("csrftoken")}
    )
    assert response.status_code == 303
    assert celery_calls.names[-1] == "app.tasks.jobs.sync_fabric"
    # ...but may not edit or delete.
    assert (await client.get(f"/fabrics/{fabric_id}/edit")).status_code == 403
    delete = await client.post(
        f"/fabrics/{fabric_id}/delete", data={"csrf_token": client.cookies.get("csrftoken")}
    )
    assert delete.status_code == 403


async def test_deleting_a_fabric_removes_its_inventory(
    client, db_session_factory, accept_any_api_key
):
    fabric_id = await create_fabric_via_web(client, "Office")
    async with db_session_factory() as db:
        db.add(Console(fabric_id=fabric_id, host_id="h1"))
        await db.commit()

    response = await client.post(
        f"/fabrics/{fabric_id}/delete", data={"csrf_token": client.cookies.get("csrftoken")}
    )
    assert response.status_code == 303
    async with db_session_factory() as db:
        assert (await db.execute(select(Fabric))).scalars().all() == []
        assert (await db.execute(select(Console))).scalars().all() == []
