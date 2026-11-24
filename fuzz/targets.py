"""The fuzz targets. Each takes raw bytes and feeds them to one parser or
guard the way untrusted input would reach it, then checks the contract:

- a parser of a Site Manager reply (`app.unifi.client`) never raises on
  anything that is shaped like the reply it expects — a broken or hostile
  answer must not be able to crash an inventory sync;
- a guard that rejects input does so with its documented error, never
  anything else;
- whatever a security guard lets through really has the property it
  promises (`safe_local_path` returns a same-site path, `normalize_email`
  returns something email-shaped).

Plain Python on purpose: `fuzz.run` drives these under Atheris, and
`tests/test_fuzz_targets.py` runs them on every OS over a seed corpus.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import yaml
from pydantic import ValidationError

from app.core import yaml_safety
from app.scheduling.cron import validate_cron_expression
from app.scheduling.targets import decode_target
from app.schemas.fabric import FabricForm
from app.schemas.user import normalize_email
from app.unifi.client import parse_devices, parse_host, parse_site
from app.web.redirects import safe_local_path


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _json(data: bytes) -> Any:
    """The reply as `httpx` would decode it; `None` for anything that isn't
    JSON at all (the client raises `SiteManagerError` for that itself)."""
    try:
        return json.loads(_text(data))
    except (ValueError, RecursionError):
        return None


def site_manager_hosts(data: bytes) -> None:
    """`/v1/hosts` items: the client only parses dicts with an `id`."""
    payload = _json(data)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            host = parse_host(item)
            assert isinstance(host.apps, list)


def site_manager_sites(data: bytes) -> None:
    """`/v1/sites` items: the client only parses dicts with both ids."""
    payload = _json(data)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict) and item.get("siteId") and item.get("hostId"):
            parse_site(item)


def site_manager_devices(data: bytes) -> None:
    """`/v1/devices` entries: one host with its device list."""
    payload = _json(data)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict):
            for device in parse_devices(item):
                assert device.mac == device.mac.lower()


def redirect(data: bytes) -> None:
    result = safe_local_path(_text(data), "/dashboard")
    assert result.startswith("/")
    assert not result.startswith("//")
    assert "\\" not in result
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in result)


def email(data: bytes) -> None:
    try:
        result = normalize_email(_text(data))
    except ValueError:
        return
    if result is not None:
        local, _, domain = result.partition("@")
        assert local and "." in domain and not any(ch.isspace() for ch in result)


def rule_yaml(data: bytes) -> None:
    """A pasted notification-rule import: parsed, or refused with a YAML
    error — never a RecursionError or a hang on deep nesting/aliases."""
    try:
        yaml_safety.load(_text(data), allow_aliases=False)
    except yaml.YAMLError:
        return


def cron(data: bytes) -> None:
    try:
        validate_cron_expression(_text(data).strip())
    except ValueError:
        return


def schedule_target(data: bytes) -> None:
    """The scheduling form's target `<select>` value (tamperable)."""
    try:
        decode_target(_text(data))
    except ValueError:
        return


def fabric_form(data: bytes) -> None:
    parts = (_text(data).split("\x00") + ["", "", ""])[:3]
    try:
        FabricForm.model_validate(
            {"name": parts[0], "description": parts[1] or None, "api_key": parts[2]}
        )
    except ValidationError:
        return


TARGETS: dict[str, Callable[[bytes], None]] = {
    "site_manager_hosts": site_manager_hosts,
    "site_manager_sites": site_manager_sites,
    "site_manager_devices": site_manager_devices,
    "redirect": redirect,
    "email": email,
    "rule_yaml": rule_yaml,
    "cron": cron,
    "schedule_target": schedule_target,
    "fabric_form": fabric_form,
}
