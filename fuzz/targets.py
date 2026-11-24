"""The fuzz targets. Each takes raw bytes and feeds them to one parser or
guard the way untrusted input would reach it, then checks the contract:

- a parser of a Site Manager reply (`app.unifi.client`) never raises on
  anything that is shaped like the reply it expects — a broken or hostile
  answer must not be able to crash an inventory sync;
- a guard that rejects input does so with its documented error, never
  anything else;
- whatever a security guard lets through really has the property it
  promises (`safe_local_path` returns a same-site path, `normalize_email`
  returns something email-shaped);
- the release-assessment readers (`app.releases`: community.ui.com,
  Reddit, web search, the AI model's answer) never raise on a hostile
  reply and only ever produce links of the kind they promise.

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
from app.releases import assess as release_assess
from app.releases.evidence import Evidence
from app.releases.products import is_newer, title_matches
from app.releases.sources import community, reddit, web
from app.releases.text import html_to_text
from app.releases.versions import normalize_version
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


def community_release(data: bytes) -> None:
    """A community.ui.com GraphQL reply: the release search, the release
    post with its comments, and the forum question search."""
    payload = _json(data)
    body = payload if isinstance(payload, dict) else {"x": payload}
    raw_items = body.get("items")
    items: list[Any] = raw_items if isinstance(raw_items, list) else [payload]
    for kind, product in (("app", "network"), ("os", "UDM-Pro"), ("firmware", "G5 Flex")):
        post = community.pick_release(kind, product, "11.0.104", items)
        if post is not None:
            assert post.url.startswith(community.SITE_URL + "/releases/")
        latest = community.pick_latest(kind, product, items)
        assert latest is None or (latest.stage or "GA") == "GA"
    evidence = Evidence()
    community.parse_release(evidence, body, "id")
    community.parse_questions(evidence, body, "11.0.104")
    for item in evidence.items:
        assert item.url is None or item.url.startswith(community.SITE_URL + "/")


def reddit_listing(data: bytes) -> None:
    """A Reddit search or comments listing: only reddit.com links come out."""
    payload = _json(data)
    for post in reddit.parse_posts(payload):
        assert post["url"].startswith(reddit.SITE_URL + "/r/")
    for body, score in reddit.parse_comments(payload):
        assert isinstance(body, str) and body
        assert score is None or isinstance(score, int)


def web_search(data: bytes) -> None:
    """A SearXNG / Brave reply: only http(s) links outside the sources
    that have their own reader come out."""
    payload = _json(data)
    for kind in ("searxng", "brave"):
        for _title, url, _snippet in web.parse_results(kind, payload):
            assert url.startswith(("http://", "https://"))
            assert "community.ui.com" not in url.split("/")[2]


def ai_answer(data: bytes) -> None:
    """The model's answer: a valid rating 1–5, or `AssessmentError`."""
    try:
        result = release_assess.parse_answer(_text(data))
    except release_assess.AssessmentError:
        return
    assert 1 <= result.risk_level <= 5
    assert len(result.issues) <= release_assess.MAX_ISSUES


def release_text(data: bytes) -> None:
    """Version strings, release titles and HTML from any source."""
    text = _text(data)
    version = normalize_version(text)
    if version is not None:
        parts = version.split(".")
        assert 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts)
        assert not is_newer(version, version)
    title_matches("firmware", text[:40], text)
    assert isinstance(html_to_text(text), str)


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
    "community_release": community_release,
    "reddit_listing": reddit_listing,
    "web_search": web_search,
    "ai_answer": ai_answer,
    "release_text": release_text,
}
