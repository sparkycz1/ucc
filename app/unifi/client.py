"""Async client for the UniFi Site Manager API (`https://api.ui.com`).

Read endpoints (`/v1/hosts`, `/v1/sites`, `/v1/devices`) are documented and
stable. The connector proxy (`/v1/connector/consoles/{hostId}/...`) forwards a
request to the console's own local API — what *writes* it accepts is not
documented by Ubiquiti, see `scripts/probe_connector.py`.

Response parsing is deliberately defensive (`.get()` everywhere, unknown
fields ignored): the payload shape "may vary by UniFi Network version" per
Ubiquiti's own spec, and a sync must never crash on one odd console.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2

BASE_URL = "https://api.ui.com"
_TIMEOUT = httpx2.Timeout(30.0, connect=10.0)


class SiteManagerError(Exception):
    """Any non-2xx answer or transport failure. `status` is 0 for a
    transport-level error (DNS, TLS, timeout)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HostInfo:
    host_id: str
    name: str | None
    model: str | None
    ip_address: str | None
    is_online: bool
    os_version: str | None
    os_update_available: str | None
    # `reportedState.version` — a platform build number that is *not* what
    # gets updated (it isn't comparable to `os_update_available`); shown as
    # secondary "UniFi Core" information only.
    core_version: str | None = None
    # UniFi's own automatic updates (`reportedState.autoUpdate`): a short
    # human-readable schedule ("weekly, day 0, 00:00") when one is set, and
    # whether it also updates applications. These run on UniFi's side,
    # outside ucc's AI gate — the UI warns about them.
    auto_update_schedule: str | None = None
    auto_update_include_apps: bool | None = None
    # (app name, version, update available) — installed applications only.
    apps: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    # `hardware.name` ("UniFi Cloud Gateway Ultra") — only a fallback for
    # the console's display name; its own entry in `/v1/devices` gives the
    # short one ("UCG Ultra").
    model_name: str | None = None


@dataclass(frozen=True)
class SiteInfo:
    site_id: str
    host_id: str
    internal_name: str | None
    name: str | None


@dataclass(frozen=True)
class DeviceInfo:
    host_id: str
    mac: str
    name: str | None
    model: str | None
    product_line: str | None
    is_online: bool
    version: str | None
    update_available: str | None
    is_console: bool
    # Site Manager's own verdict ("upToDate", "updateAvailable", ...). Kept
    # because it can say an update exists while `update_available` is empty
    # (seen on offline devices).
    firmware_status: str | None = None
    # The display name (`model`, "UCG Ultra" → "UCG-Ultra") when `model`
    # above is the `shortname` code ("UDRULT").
    model_name: str | None = None


def _dict(value: Any) -> dict[str, Any]:
    """`value` if it is a JSON object, else `{}` — a reply with a field of
    the wrong type must not crash the whole Fabric's sync."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def parse_host(raw: dict[str, Any]) -> HostInfo:
    state = _dict(raw.get("reportedState"))
    hardware = _dict(state.get("hardware"))
    apps: list[tuple[str, str | None, str | None]] = []
    for controller in _list(state.get("controllers")):
        if not isinstance(controller, dict) or not controller.get("name"):
            continue
        # Site Manager lists every application the console *could* run
        # (Access, Protect, Talk, ...) with `isInstalled: false` and an empty
        # version; only installed ones are inventory.
        if controller.get("isInstalled") is False:
            continue
        apps.append(
            (
                str(controller["name"]),
                _str(controller.get("version")),
                _str(controller.get("updateAvailable")),
            )
        )
    firmware_update = _dict(state.get("firmwareUpdate"))
    auto_update = state.get("autoUpdate")
    return HostInfo(
        host_id=str(raw["id"]),
        name=_str(state.get("name") or state.get("hostname")),
        model=_str(hardware.get("shortname") or hardware.get("name")),
        model_name=_str(hardware.get("name")) if hardware.get("shortname") else None,
        ip_address=_str(raw.get("ipAddress") or state.get("ip")),
        is_online=str(state.get("state", "")).lower() == "connected",
        # `hardware.firmwareVersion` is the UniFi OS version that updates:
        # `firmwareUpdate.latestAvailableVersion` and Site Manager's device
        # list (the console as a device) both compare against it.
        os_version=_str(hardware.get("firmwareVersion") or state.get("version")),
        os_update_available=_str(firmware_update.get("latestAvailableVersion")),
        core_version=_str(state.get("version")),
        auto_update_schedule=_auto_update_schedule(auto_update),
        auto_update_include_apps=(
            bool(auto_update["includeApplications"])
            if isinstance(auto_update, dict)
            and isinstance(auto_update.get("includeApplications"), bool)
            else None
        ),
        apps=apps,
    )


def _auto_update_schedule(auto_update: Any) -> str | None:
    """`{"frequency": "weekly", "day": 0, "hour": 0}` -> "weekly, day 0, 00:00";
    `None` when the console reports no schedule. The day number is shown as
    Site Manager reports it rather than guessed into a weekday name."""
    if not isinstance(auto_update, dict):
        return None
    schedule = auto_update.get("schedule")
    if not isinstance(schedule, dict) or not schedule.get("frequency"):
        return None
    parts = [str(schedule["frequency"])]
    if isinstance(schedule.get("day"), int):
        parts.append(f"day {schedule['day']}")
    if isinstance(schedule.get("hour"), int):
        parts.append(f"{schedule['hour']:02d}:00")
    return ", ".join(parts)


def display_model(name: str | None) -> str | None:
    """Site Manager's model name the way UniFi writes its SKUs:
    "UCG Ultra" → "UCG-Ultra", "USW Pro Max 48 PoE" → "USW-Pro-Max-48-PoE"."""
    if not name:
        return None
    return "-".join(name.split())[:100] or None


def parse_site(raw: dict[str, Any]) -> SiteInfo:
    meta = _dict(raw.get("meta"))
    return SiteInfo(
        site_id=str(raw["siteId"]),
        host_id=str(raw["hostId"]),
        internal_name=_str(meta.get("name")),
        name=_str(meta.get("desc") or meta.get("name")),
    )


def parse_devices(raw: dict[str, Any]) -> list[DeviceInfo]:
    """One `/v1/devices` entry is a host with its `devices` list."""
    host_id = str(raw.get("hostId", ""))
    result: list[DeviceInfo] = []
    for dev in _list(raw.get("devices")):
        if not isinstance(dev, dict) or not isinstance(dev.get("mac"), str) or not dev["mac"]:
            continue
        result.append(
            DeviceInfo(
                host_id=host_id,
                mac=str(dev["mac"]).lower(),
                name=_str(dev.get("name")),
                model=_str(dev.get("shortname") or dev.get("model")),
                product_line=_str(dev.get("productLine")),
                is_online=str(dev.get("status", "")).lower() == "online",
                version=_str(dev.get("version")),
                update_available=_str(dev.get("updateAvailable")),
                is_console=bool(dev.get("isConsole")),
                firmware_status=_str(dev.get("firmwareStatus")),
                model_name=(
                    display_model(_str(dev.get("model"))) if dev.get("shortname") else None
                ),
            )
        )
    return result


class SiteManagerClient:
    """One API key's view of Site Manager. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            timeout=_TIMEOUT,
            transport=transport,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )

    async def __aenter__(self) -> SiteManagerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx2.HTTPError as exc:
            raise SiteManagerError(f"{method} {path}: {exc}") from exc
        if response.status_code >= 400:
            raise SiteManagerError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:300]}",
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SiteManagerError(f"{method} {path}: response is not JSON") from exc

    async def _paginate(self, path: str) -> AsyncIterator[dict[str, Any]]:
        token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 200}
            if token:
                params["nextToken"] = token
            payload = await self._request("GET", path, params=params)
            for item in payload.get("data") or []:
                if isinstance(item, dict):
                    yield item
            token = payload.get("nextToken")
            if not token:
                return

    async def hosts(self) -> list[HostInfo]:
        return [parse_host(h) async for h in self._paginate("/v1/hosts") if h.get("id")]

    async def sites(self) -> list[SiteInfo]:
        return [
            parse_site(s)
            async for s in self._paginate("/v1/sites")
            if s.get("siteId") and s.get("hostId")
        ]

    async def devices(self) -> list[DeviceInfo]:
        result: list[DeviceInfo] = []
        async for entry in self._paginate("/v1/devices"):
            result.extend(parse_devices(entry))
        return result

    async def connector(
        self, host_id: str, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> Any:
        """Call a console's local API through the cloud connector proxy.
        `path` is the console-local path, e.g. `/proxy/network/api/s/default/stat/device`."""
        return await self._request(method, f"/v1/connector/consoles/{host_id}{path}", json=json)
