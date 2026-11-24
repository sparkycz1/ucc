"""Mirror one Fabric's inventory from Site Manager into the database.

A full replace per sync: every console/app/site/device the API returns is
upserted, and every row the API no longer returns is deleted. The database
never holds anything the operator edited by hand under a Fabric, so there
is nothing a delete could lose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decrypt_secret
from app.db.models.console import Console, ConsoleApp
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.site import Site
from app.unifi.client import DeviceInfo, HostInfo, SiteInfo, SiteManagerClient


@dataclass(frozen=True)
class SyncResult:
    consoles: int
    sites: int
    devices: int


@dataclass(frozen=True)
class Inventory:
    hosts: list[HostInfo]
    sites: list[SiteInfo]
    devices: list[DeviceInfo]


async def fetch_inventory(client: SiteManagerClient) -> Inventory:
    return Inventory(
        hosts=await client.hosts(), sites=await client.sites(), devices=await client.devices()
    )


def _apply_apps(console: Console, host: HostInfo) -> None:
    by_name = {app.name: app for app in console.apps}
    seen: set[str] = set()
    for name, version, update in host.apps:
        seen.add(name)
        app = by_name.get(name)
        if app is None:
            app = ConsoleApp(name=name)
            console.apps.append(app)
        app.version = version
        app.update_available = update
    console.apps[:] = [a for a in console.apps if a.name in seen]


async def apply_inventory(db: AsyncSession, fabric: Fabric, inventory: Inventory) -> SyncResult:
    """Write `inventory` under `fabric`. Does not commit."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(Console)
        .where(Console.fabric_id == fabric.id)
        .options(
            selectinload(Console.apps),
            selectinload(Console.sites),
            selectinload(Console.devices),
        )
    )
    consoles = {c.host_id: c for c in result.scalars().all()}

    seen_hosts: set[str] = set()
    for host in inventory.hosts:
        seen_hosts.add(host.host_id)
        console = consoles.get(host.host_id)
        if console is None:
            console = Console(
                fabric_id=fabric.id, host_id=host.host_id, apps=[], sites=[], devices=[]
            )
            db.add(console)
            consoles[host.host_id] = console
        console.name = host.name
        console.model = host.model
        console.ip_address = host.ip_address
        console.is_online = host.is_online
        console.os_version = host.os_version
        console.os_update_available = host.os_update_available
        console.core_version = host.core_version
        console.auto_update_schedule = host.auto_update_schedule
        console.auto_update_include_apps = host.auto_update_include_apps
        if host.is_online:
            console.last_seen_at = now
        _apply_apps(console, host)

    for host_id, console in list(consoles.items()):
        if host_id not in seen_hosts:
            await db.delete(console)
            del consoles[host_id]

    site_count = 0
    sites_by_host: dict[str, list[SiteInfo]] = {}
    for site_info in inventory.sites:
        sites_by_host.setdefault(site_info.host_id, []).append(site_info)
    for host_id, console in consoles.items():
        existing = {s.site_id: s for s in console.sites}
        wanted = sites_by_host.get(host_id, [])
        for info in wanted:
            site = existing.get(info.site_id)
            if site is None:
                site = Site(site_id=info.site_id)
                console.sites.append(site)
            site.internal_name = info.internal_name
            site.name = info.name
        wanted_ids = {info.site_id for info in wanted}
        console.sites[:] = [s for s in console.sites if s.site_id in wanted_ids]
        site_count += len(console.sites)

    # Flush so new sites have ids before devices reference them.
    await db.flush()

    device_count = 0
    devices_by_host: dict[str, list[DeviceInfo]] = {}
    for dev in inventory.devices:
        devices_by_host.setdefault(dev.host_id, []).append(dev)
    for host_id, console in consoles.items():
        only_site = console.sites[0] if len(console.sites) == 1 else None
        existing_devices = {d.mac: d for d in console.devices}
        wanted_devices = devices_by_host.get(host_id, [])
        for dev_info in wanted_devices:
            device = existing_devices.get(dev_info.mac)
            if device is None:
                device = Device(mac=dev_info.mac)
                console.devices.append(device)
            device.name = dev_info.name
            device.model = dev_info.model
            device.product_line = dev_info.product_line
            device.is_online = dev_info.is_online
            device.version = dev_info.version
            device.update_available = dev_info.update_available
            device.firmware_status = dev_info.firmware_status
            if only_site is not None:
                device.site = only_site
            if dev_info.is_online:
                device.last_seen_at = now
        wanted_macs = {d.mac for d in wanted_devices}
        console.devices[:] = [d for d in console.devices if d.mac in wanted_macs]
        device_count += len(console.devices)

    fabric.last_sync_at = now
    fabric.last_sync_error = None
    return SyncResult(consoles=len(consoles), sites=site_count, devices=device_count)


async def sync_fabric(db: AsyncSession, fabric: Fabric) -> SyncResult:
    """Fetch and apply. Raises `SiteManagerError` on API failure (the
    caller records it on the Fabric). Does not commit."""
    async with SiteManagerClient(decrypt_secret(fabric.api_key_encrypted)) as client:
        inventory = await fetch_inventory(client)
    return await apply_inventory(db, fabric, inventory)
