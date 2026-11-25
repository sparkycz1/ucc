"""A release's risk *for one console or device* — what it has, not the
general rating.

The AI rates a release once, for everyone (`Release.risk_level`), but it
also tags each issue with how bad it is on its own (`severity`, 1–5) and
what it affects (`affects`: device categories from `CATEGORIES`, or model
names; empty = everyone), and gives `Release.base_risk_level`: the risk
when none of the device-specific issues apply. A Protect release whose
only real problem is broken doorbells is a 4 in general, but on an NVR
with no doorbells it is its base level — and the doorbell issue is still
shown, as "doesn't apply to you".

Everything here is deterministic: the inventory is matched against the
tags with no further model call. A manual level always wins (a person
judged it), and a contextual level is never above the general one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.db.models.console import Console
from app.db.models.device import Device

# The categories the model may tag an issue with (`assess.SYSTEM_PROMPT`
# lists them). Anything else it writes is treated as a model name.
CATEGORIES = (
    "gateway",
    "switch",
    "access_point",
    "camera",
    "doorbell",
    "chime",
    "nvr",
    "viewport",
    "sensor",
    "door_access",
    "phone",
    "wireless_bridge",
    "lte",
    "power",
)

# Model text (code or display name, upper case, no spaces/dashes) → its
# category; the first matching prefix wins, so the specific ones come first.
_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nvr", ("UNVR", "ENVR", "NVR")),
    ("viewport", ("UPVIEWPORT", "VIEWPORT", "UFPVIEWPORT")),
    ("chime", ("UPCHIME", "UACHIME", "CHIME")),
    ("sensor", ("UPSENSE", "USLENV", "SENSOR")),
    ("wireless_bridge", ("UBB", "UDB", "BUILDINGBRIDGE", "DEVICEBRIDGE")),
    ("lte", ("ULTE", "UMR", "U5G")),
    ("power", ("USP", "UPS", "USPPDU", "SMARTPOWER")),
    ("phone", ("UTP", "TALK")),
    (
        "gateway",
        ("UDM", "UDR", "UCG", "UXG", "USG", "EFG", "UDW", "UX", "UGW", "DREAM", "CLOUDGATEWAY"),
    ),
    ("access_point", ("U6", "U7", "UAP", "E7", "AC", "NANOHD", "FLEXHD", "BEACONHD", "UWB")),
    ("switch", ("USW", "US", "USL", "USM", "ECS", "USPM")),
    ("camera", ("UVC", "G3", "G4", "G5", "G6", "AI")),
    # Last: "UA" would also catch "UAP".
    ("door_access", ("UA",)),
)


def _compact(text: str | None) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def device_category(
    model: str | None, model_name: str | None = None, product_line: str | None = None
) -> str | None:
    """`UDRULT`/`UCG-Ultra` → gateway, `G4 Doorbell Pro` → doorbell, ..."""
    texts = [_compact(model_name), _compact(model)]
    joined = " ".join(texts)
    # Words that beat any prefix: a "G4 Doorbell" is a doorbell, not a camera.
    for word, category in (("DOORBELL", "doorbell"), ("CHIME", "chime"), ("VIEWPORT", "viewport")):
        if word in joined:
            return category
    for category, prefixes in _PREFIXES:
        for text in texts:
            if text and any(text.startswith(prefix) for prefix in prefixes):
                # USP (SmartPower) vs USPM (Pro Max switch): the longer wins.
                if category == "power" and text.startswith("USPM"):
                    continue
                return category
    line = (product_line or "").lower()
    return {
        "protect": "camera",
        "access": "door_access",
        "talk": "phone",
    }.get(line)


@dataclass(frozen=True)
class Profile:
    """What a console (with everything adopted on it) or one device is."""

    categories: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()

    def has(self, tag: str) -> bool:
        tag = tag.strip().lower()
        if tag in CATEGORIES:
            return tag in self.categories
        wanted = _compact(tag)
        return bool(wanted) and any(
            wanted == model or (len(wanted) >= 4 and wanted in model) for model in self.models
        )


def _models(*texts: str | None) -> set[str]:
    return {_compact(t) for t in texts if t and _compact(t)}


def console_profile(console: Console) -> Profile:
    """The console itself and every device adopted on it — what an OS or
    application update on it can touch."""
    categories: set[str] = set()
    models = _models(console.model, console.model_name)
    own = device_category(console.model, console.model_name)
    if own:
        categories.add(own)
    devices: Iterable[Device] = console.devices
    for device in devices:
        category = device_category(device.model, device.model_name, device.product_line)
        if category:
            categories.add(category)
        models |= _models(device.model, device.model_name)
    return Profile(frozenset(categories), frozenset(models))


def device_profile(device: Device) -> Profile:
    """One device — what its own firmware update touches."""
    category = device_category(device.model, device.model_name, device.product_line)
    return Profile(
        frozenset({category} if category else set()),
        frozenset(_models(device.model, device.model_name)),
    )


@dataclass(frozen=True)
class ContextualRisk:
    level: int | None
    # The release's general level, for "2 here (4 in general)".
    general: int | None = None
    # Issues the AI tagged for hardware this console/device doesn't have —
    # shown either way, so nobody is surprised by them later.
    not_applicable: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lowered(self) -> bool:
        return self.level is not None and self.general is not None and self.level < self.general


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def contextual_risk(release: Any, profile: Profile | None) -> ContextualRisk:
    """`release`'s level for `profile` (`None` = no context: the general
    level). `release` is a `Release` or anything with the same rating
    attributes (a snapshot taken before a re-assessment)."""
    manual = release.manual_risk_level
    if manual is not None:
        return ContextualRisk(manual, manual)
    general = release.risk_level
    if general is None or profile is None:
        return ContextualRisk(general, general)
    issues = [i for i in (release.issues or []) if isinstance(i, dict)]
    tagged = [i for i in issues if isinstance(i.get("affects"), list) and i["affects"]]
    base = _int(getattr(release, "base_risk_level", None))
    if not tagged or base is None:
        return ContextualRisk(general, general)
    level = base
    skipped = []
    for issue in issues:
        affects = issue.get("affects") if isinstance(issue.get("affects"), list) else []
        applies = not affects or any(isinstance(t, str) and profile.has(t) for t in affects)
        severity = _int(issue.get("severity"))
        if applies:
            if severity is not None:
                level = max(level, severity)
        else:
            skipped.append(issue)
    return ContextualRisk(min(level, general), general, skipped)
