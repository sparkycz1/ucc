"""Which settings apply to one console or device: its own, else its
Fabric's, else the global ones.

Two settings resolve this way, each on its own (a device can override the
risk level and still inherit the stages):

- the acceptable risk level — above it, an update is STOP;
- the hidden release stages — a release in one is not offered as an
  update, not listed, and not rated by the AI.

A console's own settings cover what runs on it (UniFi OS and its
applications), not the devices adopted on it: a device inherits from the
Fabric directly. `None` on any level means "inherit".
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.db.models.app_settings import AppSettings
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric

# community.ui.com's release stages (its `ReleaseStage` enum), with the
# names its own site shows for them. A release with no known stage (only
# Site Manager knows of it) is never hidden.
STAGES = ("GA", "RC", "T", "U")


class Level(enum.StrEnum):
    """Where a setting in effect comes from."""

    GLOBAL = "global"
    FABRIC = "fabric"
    CONSOLE = "console"
    DEVICE = "device"


def clean_stages(values: Iterable[object] | None) -> list[str] | None:
    """A stored or submitted stage list, as known stages in `STAGES` order;
    `None` stays `None` (inherit)."""
    if values is None:
        return None
    wanted = {str(v).strip().upper() for v in values}
    return [stage for stage in STAGES if stage in wanted]


def normalize_stage(stage: str | None) -> str | None:
    return stage.strip().upper() if stage and stage.strip() else None


@dataclass(frozen=True)
class Policy:
    max_risk_level: int
    hidden_stages: frozenset[str]
    risk_from: Level = Level.GLOBAL
    stages_from: Level = Level.GLOBAL

    def hides(self, stage: str | None) -> bool:
        normalized = normalize_stage(stage)
        return normalized is not None and normalized in self.hidden_stages


def global_policy(app_settings: AppSettings) -> Policy:
    return Policy(
        app_settings.max_acceptable_risk_level,
        frozenset(clean_stages(app_settings.hidden_release_stages or []) or []),
    )


def _override(base: Policy, owner: Fabric | Console | Device | None, level: Level) -> Policy:
    if owner is None:
        return base
    risk, risk_from = base.max_risk_level, base.risk_from
    if owner.max_acceptable_risk_level is not None:
        risk, risk_from = owner.max_acceptable_risk_level, level
    stages, stages_from = base.hidden_stages, base.stages_from
    own_stages = clean_stages(owner.hidden_release_stages)
    if own_stages is not None:
        stages, stages_from = frozenset(own_stages), level
    return Policy(risk, stages, risk_from, stages_from)


def fabric_policy(fabric: Fabric | None, app_settings: AppSettings) -> Policy:
    return _override(global_policy(app_settings), fabric, Level.FABRIC)


def console_policy(console: Console, fabric: Fabric | None, app_settings: AppSettings) -> Policy:
    """For the console's UniFi OS and applications."""
    return _override(fabric_policy(fabric, app_settings), console, Level.CONSOLE)


def device_policy(device: Device, fabric: Fabric | None, app_settings: AppSettings) -> Policy:
    return _override(fabric_policy(fabric, app_settings), device, Level.DEVICE)


def has_overrides(owner: Fabric | Console | Device) -> bool:
    return owner.max_acceptable_risk_level is not None or owner.hidden_release_stages is not None


def stages_from_form(raw: Mapping[str, str], *, inherit_allowed: bool = True) -> list[str] | None:
    """The hidden stages a settings form submitted (`partials/_policy.html`):
    `None` when it says "inherit", else the ticked `hide_stage_<STAGE>`
    boxes."""
    if inherit_allowed and raw.get("stages_mode", "inherit") != "own":
        return None
    return [stage for stage in STAGES if raw.get(f"hide_stage_{stage}")]


def risk_from_form(raw: Mapping[str, str]) -> int | None:
    """The acceptable level a settings form submitted; blank = inherit.
    Raises `ValueError` for anything but blank or 1–5."""
    value = (raw.get("max_acceptable_risk_level") or "").strip()
    if not value:
        return None
    level = int(value)
    if not 1 <= level <= 5:
        raise ValueError(level)
    return level
