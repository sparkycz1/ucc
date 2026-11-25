"""The Site Manager client's parsing/pagination and the inventory sync.

No network: the client runs against an `httpx.MockTransport`, and the sync
is exercised through `apply_inventory` with hand-built inventories.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.security import encrypt_secret
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.unifi.client import (
    DeviceInfo,
    HostInfo,
    SiteInfo,
    SiteManagerClient,
    SiteManagerError,
    parse_devices,
    parse_host,
    parse_site,
)
from app.unifi.sync import Inventory, apply_inventory

_HOST = {
    "id": "70A7:123",
    "ipAddress": "203.0.113.7",
    "reportedState": {
        "name": "HQ UDM",
        "state": "connected",
        "version": "4.1.13",
        "hardware": {"shortname": "UDMPRO", "firmwareVersion": "6.0.17"},
        "autoUpdate": {
            "includeApplications": True,
            "schedule": {"day": 0, "frequency": "weekly", "hour": 3},
        },
        "controllers": [
            {"name": "network", "version": "9.0.114", "updateAvailable": "9.1.120"},
            {"name": "protect", "version": "5.1.0", "isInstalled": True},
            {"name": "talk", "version": "", "isInstalled": False},
            {"version": "no-name-is-skipped"},
        ],
    },
}


def test_parse_host_reads_os_and_app_versions():
    host = parse_host(_HOST)
    assert host.host_id == "70A7:123"
    assert host.name == "HQ UDM"
    assert host.model == "UDMPRO"
    assert host.is_online is True
    # UniFi OS is the hardware firmware version (what updates compare to);
    # reportedState.version is a separate platform build.
    assert host.os_version == "6.0.17"
    assert host.core_version == "4.1.13"
    # `talk` is listed by Site Manager but not installed — not inventory.
    assert host.apps == [("network", "9.0.114", "9.1.120"), ("protect", "5.1.0", None)]
    assert host.auto_update_schedule == "weekly, day 0, 03:00"
    assert host.auto_update_include_apps is True


def test_parse_host_tolerates_a_sparse_payload():
    host = parse_host({"id": "x"})
    assert host.name is None and host.os_version is None and host.apps == []
    assert host.auto_update_schedule is None and host.auto_update_include_apps is None
    assert host.is_online is False


def test_parse_site_and_devices():
    site = parse_site({"siteId": "s1", "hostId": "h1", "meta": {"name": "default", "desc": "HQ"}})
    assert site == SiteInfo(site_id="s1", host_id="h1", internal_name="default", name="HQ")

    devices = parse_devices(
        {
            "hostId": "h1",
            "devices": [
                {
                    "mac": "AA:BB:CC:00:00:01",
                    "name": "AP Lobby",
                    "shortname": "U6-Pro",
                    "status": "online",
                    "version": "6.6.77",
                    "updateAvailable": "6.7.10",
                },
                {
                    "mac": "AA:BB:CC:00:00:02",
                    "shortname": "US48PRO",
                    "status": "offline",
                    "version": "",
                    "firmwareStatus": "updateAvailable",
                    "updateAvailable": "",
                },
                {"name": "no mac — skipped"},
            ],
        }
    )
    assert len(devices) == 2
    assert devices[0].mac == "aa:bb:cc:00:00:01"  # normalized
    assert devices[0].model == "U6-Pro"
    assert devices[0].update_available == "6.7.10"
    # Offline devices can report "update available" with no versions at all.
    assert devices[1].version is None and devices[1].update_available is None
    assert devices[1].firmware_status == "updateAvailable"


async def test_client_follows_next_token_pagination_and_sends_the_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "nextToken" not in request.url.params:
            return httpx.Response(200, json={"data": [_HOST], "nextToken": "p2"})
        return httpx.Response(200, json={"data": [{**_HOST, "id": "second"}]})

    async with SiteManagerClient("my-key", transport=httpx.MockTransport(handler)) as client:
        hosts = await client.hosts()

    assert [h.host_id for h in hosts] == ["70A7:123", "second"]
    assert all(r.headers["X-API-Key"] == "my-key" for r in seen)
    assert seen[1].url.params["nextToken"] == "p2"


async def test_client_raises_with_the_http_status():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "bad"}))
    async with SiteManagerClient("bad", transport=transport) as client:
        with pytest.raises(SiteManagerError) as exc_info:
            await client.hosts()
    assert exc_info.value.status == 401


def _inventory(*, device_version: str = "6.6.77", with_second_host: bool = True) -> Inventory:
    hosts = [
        HostInfo(
            host_id="h1",
            name="HQ",
            model="UDMPRO",
            ip_address=None,
            is_online=True,
            os_version="4.1.13",
            os_update_available=None,
            apps=[("network", "9.0.114", "9.1.120")],
        )
    ]
    if with_second_host:
        hosts.append(
            HostInfo(
                host_id="h2",
                name="Branch",
                model="UCG",
                ip_address=None,
                is_online=False,
                os_version="4.0.0",
                os_update_available="4.1.13",
            )
        )
    return Inventory(
        hosts=hosts,
        sites=[SiteInfo(site_id="s1", host_id="h1", internal_name="default", name="HQ")],
        devices=[
            DeviceInfo(
                host_id="h1",
                mac="aa:bb:cc:00:00:01",
                name="AP Lobby",
                model="U6-Pro",
                product_line="network",
                is_online=True,
                version=device_version,
                update_available="6.7.10",
                is_console=False,
            )
        ],
    )


async def _load(db: Any, fabric: Fabric) -> list[Console]:
    result = await db.execute(
        select(Console)
        .where(Console.fabric_id == fabric.id)
        .options(
            selectinload(Console.apps),
            selectinload(Console.sites),
            selectinload(Console.devices).selectinload(Device.site),
        )
        .order_by(Console.host_id)
    )
    return list(result.scalars().all())


async def test_apply_inventory_creates_then_updates_then_prunes(db_session_factory):
    async with db_session_factory() as db:
        fabric = Fabric(name="F", api_key_encrypted=encrypt_secret("k"))
        db.add(fabric)
        await db.commit()

        result = await apply_inventory(db, fabric, _inventory())
        await db.commit()
        assert (result.consoles, result.sites, result.devices) == (2, 1, 1)
        assert fabric.last_sync_at is not None

    async with db_session_factory() as db:
        fabric = (await db.execute(select(Fabric))).scalar_one()
        consoles = await _load(db, fabric)
        hq = consoles[0]
        assert hq.os_version == "4.1.13"
        assert [(a.name, a.update_available) for a in hq.apps] == [("network", "9.1.120")]
        # A single-site console's devices are attached to that site.
        assert hq.devices[0].site is not None and hq.devices[0].site.site_id == "s1"
        device_id = hq.devices[0].id

        # Second sync: device upgraded, branch console gone.
        await apply_inventory(
            db, fabric, _inventory(device_version="6.7.10", with_second_host=False)
        )
        await db.commit()

    async with db_session_factory() as db:
        fabric = (await db.execute(select(Fabric))).scalar_one()
        consoles = await _load(db, fabric)
        assert [c.host_id for c in consoles] == ["h1"]
        device = consoles[0].devices[0]
        assert device.id == device_id  # updated in place, not recreated
        assert device.version == "6.7.10"


def test_model_display_names_come_from_site_manager():
    from app.unifi.client import display_model

    assert display_model("UCG Ultra") == "UCG-Ultra"
    assert display_model("USW  Pro Max 48 PoE") == "USW-Pro-Max-48-PoE"
    assert display_model("") is None and display_model(None) is None
    host = parse_host(
        {"id": "h", "reportedState": {"hardware": {"shortname": "UDRULT",
                                                    "name": "UniFi Cloud Gateway Ultra"}}}
    )  # fmt: skip
    assert host.model == "UDRULT" and host.model_name == "UniFi Cloud Gateway Ultra"
    devices = parse_devices(
        {"hostId": "h", "devices": [
            {"mac": "aa:00", "shortname": "UDRULT", "model": "UCG Ultra", "isConsole": True},
            {"mac": "aa:01", "model": "U7 Pro"},
        ]}
    )  # fmt: skip
    assert (devices[0].model, devices[0].model_name) == ("UDRULT", "UCG-Ultra")
    # No shortname: `model` already holds the only name there is.
    assert (devices[1].model, devices[1].model_name) == ("U7 Pro", None)


async def test_sync_names_the_console_after_its_own_device_entry(db_session_factory):
    async with db_session_factory() as db_session:
        await _names_case(db_session)


async def _names_case(db_session: Any) -> None:
    fabric = Fabric(name="Names", api_key_encrypted=encrypt_secret("k"))
    db_session.add(fabric)
    await db_session.flush()
    hosts = [
        HostInfo(host_id="h1", name="A", model="UDRULT", ip_address=None, is_online=True,
                 os_version="4.3.6", os_update_available=None,
                 model_name="UniFi Cloud Gateway Ultra"),
        HostInfo(host_id="h2", name="B", model="UDMPRO", ip_address=None, is_online=True,
                 os_version="4.3.6", os_update_available=None,
                 model_name="UniFi Dream Machine Pro"),
    ]  # fmt: skip
    devices = [
        DeviceInfo(host_id="h1", mac="aa:00", name="gw", model="UDRULT", product_line="network",
                   is_online=True, version="4.3.6", update_available=None, is_console=True,
                   model_name="UCG-Ultra"),
    ]  # fmt: skip
    await apply_inventory(db_session, fabric, Inventory(hosts=hosts, sites=[], devices=devices))
    await db_session.flush()
    consoles = {
        c.host_id: c
        for c in (
            await db_session.execute(select(Console).where(Console.fabric_id == fabric.id))
        ).scalars()
    }
    assert consoles["h1"].model_name == "UCG-Ultra"
    # No device entry for it: the long hardware name.
    assert consoles["h2"].model_name == "UniFi Dream Machine Pro"
