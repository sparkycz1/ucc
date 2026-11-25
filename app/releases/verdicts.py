"""Pending updates and the GO/STOP recommendation for each.

A pending update is anything synced that could move to a newer version:
what Site Manager itself offers (a console's UniFi OS, an application, a
device's firmware), and — where Site Manager offers nothing — a newer GA
release on the same line on community.ui.com (`LatestRelease`), since
UniFi rolls releases out gradually and a console can say "up to date" for
days after a release is public.

The verdict compares the release's risk level *for that console or device*
(`app.releases.context`: manual if set, else the AI's, lowered when its
problems are tagged for hardware the console doesn't have) with the
acceptable level: `Fabric.max_acceptable_risk_level` if the
Fabric overrides it, else `AppSettings.max_acceptable_risk_level`.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.release import LatestRelease, Release, ReleaseKind
from app.releases.context import (
    ContextualRisk,
    Profile,
    console_profile,
    contextual_risk,
    device_profile,
)
from app.releases.products import is_newer
from app.releases.versions import normalize_version


class Verdict(enum.StrEnum):
    GO = "go"
    STOP = "stop"
    # Not rated yet (queued, failed, or assessment is turned off).
    PENDING = "pending"
    # Site Manager says there is an update but not to which version.
    UNKNOWN = "unknown"


class TargetSource(enum.StrEnum):
    SITE_MANAGER = "site_manager"
    COMMUNITY = "community"
    # Not an update: the version running now (`installed_targets`).
    INSTALLED = "installed"


@dataclass(frozen=True)
class UpdateTarget:
    """One thing that could be updated, and to what."""

    kind: str
    product: str
    current_version: str | None
    # Normalized; `None` = an update exists but its version is unknown.
    target_version: str | None
    source: TargetSource
    console: Console
    device: Device | None = None
    app_name: str | None = None

    @property
    def key(self) -> tuple[str, str, str] | None:
        """The release this target is an update to — `None` when it can't be
        identified (no target version, or no model to tell the product)."""
        if self.target_version is None or not self.product:
            return None
        return (self.kind, self.product, self.target_version)

    @property
    def label(self) -> str:
        if self.device is not None:
            return self.device.name or self.device.mac
        name = self.console.name or self.console.host_id
        if self.kind == ReleaseKind.OS:
            return f"{name} — UniFi OS"
        return f"{name} — {(self.app_name or self.product).capitalize()}"


    @property
    def profile(self) -> Profile:
        """What this update can touch: the device itself for firmware, the
        console and everything on it for UniFi OS and applications."""
        if self.device is not None:
            return device_profile(self.device)
        return console_profile(self.console)


@dataclass(frozen=True)
class TargetVerdict:
    target: UpdateTarget
    verdict: Verdict
    release: Release | None
    max_risk_level: int
    # The release's level for this target (`app.releases.context`).
    risk: ContextualRisk = ContextualRisk(None)


def os_product(console: Console) -> str | None:
    return console.model.strip() if console.model and console.model.strip() else None


def device_product(device: Device) -> str | None:
    return device.model.strip() if device.model and device.model.strip() else None


def installed_keys(consoles: Iterable[Console]) -> set[tuple[str, str, str]]:
    """(kind, product, installed version) of everything synced — what
    `LatestRelease` rows are looked up (and refreshed) by."""
    keys: set[tuple[str, str, str]] = set()
    for console in consoles:
        product = os_product(console)
        current = normalize_version(console.os_version)
        if product and current:
            keys.add((ReleaseKind.OS.value, product, current))
        for app in console.apps:
            current = normalize_version(app.version)
            if current:
                keys.add((ReleaseKind.APP.value, app.name, current))
        for device in console.devices:
            product, current = device_product(device), normalize_version(device.version)
            if product and current:
                keys.add((ReleaseKind.FIRMWARE.value, product, current))
    return keys


async def load_latest(
    db: AsyncSession, keys: set[tuple[str, str, str]]
) -> dict[tuple[str, str, str], LatestRelease]:
    if not keys:
        return {}
    rows = (
        await db.execute(
            select(LatestRelease).where(
                tuple_(
                    LatestRelease.kind, LatestRelease.product, LatestRelease.current_version
                ).in_(list(keys))
            )
        )
    ).scalars()
    return {(r.kind, r.product, r.current_version): r for r in rows}


def _target(
    *,
    kind: str,
    product: str,
    current: str | None,
    offered: str | None,
    offered_flag: bool,
    latest: dict[tuple[str, str, str], LatestRelease],
    console: Console,
    device: Device | None = None,
    app_name: str | None = None,
) -> UpdateTarget | None:
    normalized_current = normalize_version(current)
    normalized_offered = normalize_version(offered)
    if normalized_offered and normalized_offered != normalized_current:
        return UpdateTarget(
            kind,
            product,
            current,
            normalized_offered,
            TargetSource.SITE_MANAGER,
            console,
            device,
            app_name,
        )
    if normalized_current and product:
        row = latest.get((kind, product, normalized_current))
        if row is not None and row.version and is_newer(row.version, normalized_current):
            return UpdateTarget(
                kind,
                product,
                current,
                row.version,
                TargetSource.COMMUNITY,
                console,
                device,
                app_name,
            )
    if offered_flag:
        return UpdateTarget(
            kind, product, current, None, TargetSource.SITE_MANAGER, console, device, app_name
        )
    return None


def update_targets(
    consoles: Iterable[Console], latest: dict[tuple[str, str, str], LatestRelease]
) -> list[UpdateTarget]:
    """Every pending update under `consoles` (with apps and devices loaded)."""
    targets: list[UpdateTarget] = []
    for console in consoles:
        target = _target(
            kind=ReleaseKind.OS.value,
            product=os_product(console) or "",
            current=console.os_version,
            offered=console.os_update_available,
            offered_flag=False,
            latest=latest,
            console=console,
        )
        if target:
            targets.append(target)
        for app in console.apps:
            target = _target(
                kind=ReleaseKind.APP.value,
                product=app.name,
                current=app.version,
                offered=app.update_available,
                offered_flag=False,
                latest=latest,
                console=console,
                app_name=app.name,
            )
            if target:
                targets.append(target)
        for device in console.devices:
            target = _target(
                kind=ReleaseKind.FIRMWARE.value,
                product=device_product(device) or "",
                current=device.version,
                offered=device.update_available,
                offered_flag=device.has_update,
                latest=latest,
                console=console,
                device=device,
            )
            if target:
                targets.append(target)
    return targets


async def load_releases(
    db: AsyncSession, targets: Iterable[UpdateTarget]
) -> dict[tuple[str, str, str], Release]:
    keys = {t.key for t in targets if t.key is not None}
    if not keys:
        return {}
    rows = (
        await db.execute(
            select(Release).where(
                tuple_(Release.kind, Release.product, Release.version).in_(list(keys))
            )
        )
    ).scalars()
    return {(r.kind, r.product, r.version): r for r in rows}


def max_risk_for(fabric: Fabric, global_level: int) -> int:
    if fabric.max_acceptable_risk_level is not None:
        return fabric.max_acceptable_risk_level
    return global_level


def verdict_for(
    release: Release | None, max_risk_level: int, level: int | None = None
) -> Verdict:
    """GO/STOP for `release` at `level` (default: its general level)."""
    if release is None:
        return Verdict.PENDING
    if release.is_allowed:
        return Verdict.GO
    if level is None:
        level = release.effective_risk_level
    if level is None:
        return Verdict.PENDING
    return Verdict.GO if level <= max_risk_level else Verdict.STOP


def verdicts(
    targets: Iterable[UpdateTarget],
    releases: dict[tuple[str, str, str], Release],
    max_risk_level: int,
) -> list[TargetVerdict]:
    out = []
    for target in targets:
        key = target.key
        if key is None:
            out.append(TargetVerdict(target, Verdict.UNKNOWN, None, max_risk_level))
            continue
        release = releases.get(key)
        risk = ContextualRisk(None)
        if release is not None:
            risk = contextual_risk(release, target.profile)
        out.append(
            TargetVerdict(
                target,
                verdict_for(release, max_risk_level, risk.level),
                release,
                max_risk_level,
                risk,
            )
        )
    return out


def auto_update_applies(target: UpdateTarget) -> bool:
    """UniFi's own auto-update on this console would install `target` on
    its schedule, whatever ucc says: UniFi OS always, applications only
    with "include applications". Device firmware isn't covered by it."""
    console = target.console
    if not console.auto_update_schedule or target.device is not None:
        return False
    if target.kind == ReleaseKind.OS:
        return True
    return target.kind == ReleaseKind.APP and bool(console.auto_update_include_apps)


def is_console_entry(console: Console, device: Device) -> bool:
    """Site Manager lists a console among its own devices too; that entry's
    "firmware" is the console's UniFi OS, already its own target."""
    return bool(device.model and console.model and device.model.strip() == console.model.strip())


def installed_targets(consoles: Iterable[Console]) -> list[UpdateTarget]:
    """What runs now, as targets keyed on the installed version — rated
    like an update, so a version that turns out bad after it was installed
    is flagged (`TargetSource.INSTALLED`)."""
    out: list[UpdateTarget] = []
    for console in consoles:
        version = normalize_version(console.os_version)
        product = os_product(console)
        if product and version:
            out.append(
                UpdateTarget(
                    ReleaseKind.OS.value,
                    product,
                    console.os_version,
                    version,
                    TargetSource.INSTALLED,
                    console,
                )
            )
        for app in console.apps:
            version = normalize_version(app.version)
            if version:
                out.append(
                    UpdateTarget(
                        ReleaseKind.APP.value,
                        app.name,
                        app.version,
                        version,
                        TargetSource.INSTALLED,
                        console,
                        app_name=app.name,
                    )
                )
        for device in console.devices:
            version, product = normalize_version(device.version), device_product(device)
            if product and version and not is_console_entry(console, device):
                out.append(
                    UpdateTarget(
                        ReleaseKind.FIRMWARE.value,
                        product,
                        device.version,
                        version,
                        TargetSource.INSTALLED,
                        console,
                        device,
                    )
                )
    return out
