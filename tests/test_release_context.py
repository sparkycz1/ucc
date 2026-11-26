"""0.3.0: release risk in context (what a console/device actually has),
installed-version risk, UniFi auto-update risk, rating history, OS and
firmware in the Releases catalogue, and the Status page.

No network and no real broker: community.ui.com, the AI provider and the
Celery ping are fakes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from celery.schedules import crontab
from sqlalchemy import select

from app.core.app_settings import get_or_create_app_settings
from app.core.security import encrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig, AiProviderKind
from app.db.models.console import Console, ConsoleApp
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.job_run import JobRun
from app.db.models.release import LatestRelease, Release, ReleaseRating
from app.db.models.role import Permission
from app.releases.assess import parse_answer
from app.releases.context import (
    Profile,
    console_profile,
    contextual_risk,
    device_category,
)
from app.releases.evidence import CommunityRelease, Evidence
from app.releases.history import RatingSnapshot
from app.releases.service import claim_unrated, share_rating
from app.releases.sources.community import CommunityClient
from app.releases.verdicts import (
    TargetSource,
    Verdict,
    auto_update_applies,
    installed_targets,
    update_targets,
    verdicts,
)
from app.tasks import job_runs, release_jobs

# The model situation from the request: a Protect release that breaks
# doorbells — an NVR without doorbells shouldn't be stopped by it.
DOORBELL_ISSUES = [
    {"title": "Doorbells reboot", "detail": "many reports", "severity": 4, "affects": ["doorbell"]},
    {"title": "Slow timeline", "detail": "", "severity": 2, "affects": []},
]


def _nvr(*devices: Device) -> Console:
    console = Console(
        host_id=f"h-{uuid.uuid4().hex[:6]}",
        name="NVR",
        model="UNVR",
        model_name="UNVR",
        os_version="4.1.13",
        auto_update_schedule=None,
    )
    console.apps = [ConsoleApp(name="protect", version="5.1.0", update_available="5.2.0")]
    console.devices = list(devices)
    return console


def _camera(model: str, name: str | None = None) -> Device:
    return Device(
        id=uuid.uuid4(), mac=uuid.uuid4().hex[:12], model=model, model_name=name,
        product_line="protect", version="4.70.1",
    )  # fmt: skip


def _protect(**kwargs: Any) -> Release:
    values: dict[str, Any] = {
        "risk_level": 4,
        "base_risk_level": 2,
        "issues": DOORBELL_ISSUES,
    }
    values.update(kwargs)
    return Release(kind="app", product="protect", version="5.2.0", **values)


# --- device categories and the contextual level -----------------------------------


@pytest.mark.parametrize(
    ("model", "name", "line", "expected"),
    [
        ("UDRULT", "UCG-Ultra", "network", "gateway"),
        ("UDMPRO", "UDM-Pro", None, "gateway"),
        ("USPM48P", "USW-Pro-Max-48-PoE", "network", "switch"),
        ("US24P250", "US-24-PoE-250W", "network", "switch"),
        ("U7PRO", "U7-Pro", "network", "access_point"),
        ("UAP-AC-Pro", None, "network", "access_point"),
        ("UVC-G4-DOORBELL", "G4 Doorbell Pro", "protect", "doorbell"),
        ("UVC-G5-Flex", "G5 Flex", "protect", "camera"),
        ("UNVR", None, None, "nvr"),
        ("UA-Hub", None, "access", "door_access"),
        ("UP-Chime", None, "protect", "chime"),
        ("XYZ", None, "protect", "camera"),
        ("XYZ", None, None, None),
    ],
)
def test_device_category(model: str, name: str | None, line: str | None, expected: str) -> None:
    assert device_category(model, name, line) == expected


def test_doorbell_problem_lowers_the_level_where_there_are_no_doorbells() -> None:
    release = _protect()
    without = contextual_risk(release, console_profile(_nvr(_camera("UVC-G5-Flex", "G5 Flex"))))
    assert without.level == 2 and without.general == 4 and without.lowered
    assert [i["title"] for i in without.not_applicable] == ["Doorbells reboot"]

    with_bell = contextual_risk(
        release, console_profile(_nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro")))
    )
    assert with_bell.level == 4 and not with_bell.lowered and with_bell.not_applicable == []

    # A model name as the tag works too.
    tagged = _protect(issues=[{"title": "x", "severity": 5, "affects": ["G4 Doorbell Pro"]}])
    assert contextual_risk(tagged, console_profile(_nvr())).level == 2
    assert (
        contextual_risk(
            tagged, console_profile(_nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro")))
        ).level
        == 4
    )


def test_contextual_level_falls_back_to_the_general_one() -> None:
    nvr = console_profile(_nvr())
    # Rated before 0.3.0: no base level, no tags.
    assert contextual_risk(_protect(base_risk_level=None), nvr).level == 4
    assert contextual_risk(_protect(issues=[{"title": "a"}]), nvr).level == 4
    # A person's level always wins.
    assert contextual_risk(_protect(manual_risk_level=3), nvr).level == 3
    # Never above the general level, and no context = the general level.
    assert contextual_risk(_protect(risk_level=3, base_risk_level=2), nvr).level == 2
    assert contextual_risk(_protect(), None).level == 4
    assert not Profile().has("doorbell")


def test_answer_tags_are_validated() -> None:
    result = parse_answer(
        json.dumps({
            "risk_level": 3, "base_risk_level": 5, "summary": "s",
            "issues": [{"title": "t", "severity": 4, "affects": ["doorbell", " ", 7, "x" * 99]}],
        })
    )  # fmt: skip
    # base is clamped to the general level; junk tags dropped, long ones cut.
    assert result.base_risk_level == 3
    assert result.issues[0].affects == ["doorbell", "x" * 60]
    assert result.issues[0].severity == 4


def test_verdicts_use_the_level_in_context() -> None:
    plain, belled = _nvr(), _nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro"))
    targets = update_targets([plain, belled], {})
    release = _protect()
    found = {
        v.target.console.host_id: v
        for v in verdicts(targets, {("app", "protect", "5.2.0"): release}, 2)
        if v.target.kind == "app"
    }
    assert found[plain.host_id].verdict is Verdict.GO
    assert found[plain.host_id].risk.lowered
    assert found[belled.host_id].verdict is Verdict.STOP


def test_installed_targets_and_auto_update() -> None:
    console = _nvr(_camera("UVC-G5-Flex"))
    # The console's own entry in the device list is its UniFi OS, not firmware.
    console.devices.append(Device(id=uuid.uuid4(), mac="own", model="UNVR", version="4.1.13"))
    running = installed_targets([console])
    assert {(t.kind, t.product, t.target_version) for t in running} == {
        ("os", "UNVR", "4.1.13"),
        ("app", "protect", "5.1.0"),
        ("firmware", "UVC-G5-Flex", "4.70.1"),
    }
    assert all(t.source is TargetSource.INSTALLED for t in running)

    pending = update_targets([console], {})
    app_target = next(t for t in pending if t.kind == "app")
    assert not auto_update_applies(app_target)
    console.auto_update_schedule = "weekly, day 0, 00:00"
    console.auto_update_include_apps = False
    assert not auto_update_applies(app_target)
    console.auto_update_include_apps = True
    assert auto_update_applies(app_target)


# --- shared ratings, claims, history ---------------------------------------------------


async def test_models_on_one_post_share_one_assessment(db_session_factory: Any) -> None:
    async with db_session_factory() as db:
        rows = [
            Release(kind="firmware", product=model, version="7.2.1", community_release_id="p1")
            for model in ("USW-24", "USW-48", "USW-Flex")
        ]
        db.add_all(rows)
        await db.flush()
        # One claim per post; the others are marked queued so nothing else
        # claims them while it runs.
        assert len(claim_unrated(rows)) == 1
        assert all(r.assessment_queued_at is not None for r in rows)
        rows[0].risk_level, rows[0].reason = 3, "loops"
        rows[0].assessed_at = datetime.now(UTC)
        shared = await share_rating(db, rows[0])
        assert {r.product for r in shared} == {"USW-48", "USW-Flex"}
        assert all(r.risk_level == 3 and r.reason == "loops" for r in shared)
        assert all(r.assessment_queued_at is None for r in shared)


async def test_manual_decisions_write_history(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    async with db_session_factory() as db:
        release = Release(kind="app", product="protect", version="5.2.0", risk_level=4)
        db.add(release)
        await db.commit()
    await client.get(f"/releases/{release.id}")
    csrf = client.cookies.get("csrftoken")
    await client.post(
        f"/releases/{release.id}/risk",
        data={"risk_level": "2", "note": "tested", "csrf_token": csrf},
    )
    await client.post(f"/releases/{release.id}/allow", data={"csrf_token": csrf})
    await client.post(f"/releases/{release.id}/revoke", data={"csrf_token": csrf})
    async with db_session_factory() as db:
        entries = list(
            (
                await db.execute(
                    select(ReleaseRating).where(ReleaseRating.release_id == release.id)
                )
            ).scalars()
        )
        assert sorted(e.source for e in entries) == ["allow_revoked", "allowed", "manual"]
        manual = next(e for e in entries if e.source == "manual")
        assert manual.risk_level == 2 and manual.reason == "tested" and manual.actor
    page = await client.get(f"/releases/{release.id}")
    assert "Rating history" in page.text and "Manual level" in page.text


# --- the assessment job: history, sharing, what turned bad -----------------------------


@pytest.fixture
def jobs_db(db_session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "AsyncSessionLocal", db_session_factory)
    return db_session_factory


async def _model(factory: Any) -> None:
    async with factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.ANTHROPIC, enabled=True, api_key_encrypted=encrypt_secret("k")
        )
        db.add(provider)
        await db.flush()
        model = AiModel(provider_id=provider.id, model_id="fake-model", enabled=True)
        db.add(model)
        await db.flush()
        (await get_or_create_app_settings(db)).release_assessment_model_id = model.id
        await db.commit()


class _Answer:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = json.dumps(answer)

    async def send(self, messages: Any, tools: Any, model: str, system_prompt: str) -> Any:
        from app.ai.base import ChatTurnResult

        assert "doorbell" in system_prompt and "base_risk_level" in system_prompt
        return ChatTurnResult(text=self.answer, input_tokens=10, output_tokens=5)

    def build_user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}


async def test_assessment_notifies_what_turned_bad_in_context(
    jobs_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _model(jobs_db)
    async with jobs_db() as db:
        fabric = Fabric(name="Site", api_key_encrypted=encrypt_secret("k"))
        db.add(fabric)
        await db.flush()
        # Runs 5.2.0 already (installed), with UniFi auto-update incl. apps,
        # and a doorbell — the doorbell issue applies here.
        running = _nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro"))
        running.apps = [ConsoleApp(name="protect", version="5.2.0")]
        running.fabric_id = fabric.id
        # Offered 5.2.0, auto-update with apps on, no doorbell.
        pending = _nvr(_camera("UVC-G5-Flex", "G5 Flex"))
        pending.auto_update_schedule = "weekly, day 0, 00:00"
        pending.auto_update_include_apps = True
        pending.fabric_id = fabric.id
        # Offered 5.2.0 with a doorbell: STOP, and auto-update would install it.
        belled = _nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro"))
        belled.name = "Belled NVR"
        belled.auto_update_schedule = "weekly, day 0, 00:00"
        belled.auto_update_include_apps = True
        belled.fabric_id = fabric.id
        db.add_all([running, pending, belled])
        release = Release(
            kind="app", product="protect", version="5.2.0", risk_level=2, community_release_id="p9"
        )
        db.add(release)
        await db.commit()

    async def _evidence(app_settings: Any, rel: Release) -> Evidence:
        return Evidence(
            release=CommunityRelease(
                id="p9", title="UniFi Protect Application", version="5.2.0", stage="GA",
                url="https://community.ui.com/releases/p/p9", created_at="2026-11-01T00:00:00Z",
                notes="n",
            )
        )  # fmt: skip

    sent: list[tuple[str, dict[str, Any]]] = []

    async def _notify(db: Any, event: Any, *, fabric: Any = None, context: Any = None) -> None:
        sent.append((event.value, context or {}))

    async def _publish(fabric_id: str, kind: str) -> None:
        return None

    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence)
    monkeypatch.setattr(
        release_jobs,
        "build_client",
        lambda provider: _Answer(
            {"risk_level": 4, "base_risk_level": 2, "reason": "doorbells reboot",
             "summary": "Doorbells reboot.", "issues": DOORBELL_ISSUES}
        ),
    )  # fmt: skip
    monkeypatch.setattr(release_jobs, "notify", _notify)
    monkeypatch.setattr(release_jobs, "publish_fabric_event", _publish)

    result = await release_jobs._assess_release(str(release.id))
    assert result["ok"] is True
    events = [e for e, _c in sent]
    assert events.count("release.assessed") == 1
    # 2 → 4 in general.
    changed = next(c for e, c in sent if e == "release.risk_changed")
    assert (changed["previous_risk_level"], changed["risk_level"]) == ("2", "4")
    # Only the doorbell NVR is blocked; the plain one stays GO at level 2.
    blocked = [c for e, c in sent if e == "update.blocked"]
    assert len(blocked) == 1 and "Belled NVR" in blocked[0]["target_name"]
    auto = [c for e, c in sent if e == "update.auto_update_risk"]
    assert len(auto) == 1 and "Belled NVR" in auto[0]["target_name"]
    assert auto[0]["schedule"] == "weekly, day 0, 00:00"
    at_risk = [c for e, c in sent if e == "release.installed_at_risk"]
    assert len(at_risk) == 1 and at_risk[0]["count"] == "1"

    async with jobs_db() as db:
        stored = await db.get(Release, release.id)
        assert stored is not None and stored.base_risk_level == 2
        assert stored.issues and stored.issues[0]["affects"] == ["doorbell"]
        history = list((await db.execute(select(ReleaseRating))).scalars())
        assert [(h.source, h.risk_level) for h in history] == [("ai", 4)]

    # Re-assessing with the same answer turns nothing bad again.
    sent.clear()
    await release_jobs._assess_release(str(release.id))
    assert not [e for e, _c in sent if e != "release.assessed"]


def test_snapshot_reads_like_a_release() -> None:
    snapshot = RatingSnapshot.of(_protect())
    nvr = console_profile(_nvr())
    assert contextual_risk(snapshot, nvr).level == 2
    assert snapshot.effective_risk_level == 4 and not snapshot.allowed


# --- the catalogue: UniFi OS and firmware lines ----------------------------------------


async def test_catalog_includes_installed_os_and_firmware_lines(
    jobs_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.releases import service

    async with jobs_db() as db:
        fabric = Fabric(name="F", api_key_encrypted=encrypt_secret("k"))
        db.add(fabric)
        await db.flush()
        console = Console(host_id="h", name="GW", model="UDRULT", os_version="4.3.6")
        console.fabric_id = fabric.id
        console.apps = []
        console.devices = [
            Device(mac="a", model="USW-24", version="7.1.26"),
            Device(mac="b", model="USW-48", version="7.1.26"),
            Device(mac="c", model="UDRULT", version="4.3.6"),  # the console itself
        ]
        db.add(console)
        now = datetime.now(UTC)
        for product, kind, current, title in (
            ("UDRULT", "os", "4.3.6", "UniFi OS - Cloud Gateways"),
            ("USW-24", "firmware", "7.1.26", "UniFi Switch"),
            ("USW-48", "firmware", "7.1.26", "UniFi Switch"),
        ):
            db.add(
                LatestRelease(
                    kind=kind, product=product, current_version=current, version=current,
                    title=title, url="https://community.ui.com/x", checked_at=now,
                )
            )  # fmt: skip
        await db.commit()

    def _item(title: str, version: str, n: int) -> dict[str, Any]:
        return {"id": f"{title[:3]}{n}", "slug": "s", "title": title, "version": version,
                "stage": "GA", "createdAt": "2026-11-01T00:00:00Z"}  # fmt: skip

    async def _search(self: Any, term: str, limit: int = 25) -> list[Any]:
        if term == "UniFi Switch":
            versions = ("7.2.1", "7.1.26", "7.2.5", "7.0.50", "6.9.9")
            return [_item("UniFi Switch", v, n) for n, v in enumerate(versions)]
        if term == "UniFi OS - Cloud Gateways":
            return [_item(term, v, n) for n, v in enumerate(("4.4.6", "4.3.6"))]
        return []

    monkeypatch.setattr(CommunityClient, "search", _search)
    await release_jobs._refresh_release_catalog()
    async with jobs_db() as db:
        groups = await service.catalog(db)
        switch = next(g for g in groups if g.title == "UniFi Switch")
        assert switch.kind == "firmware" and switch.products == ["USW-24", "USW-48"]
        assert not [g for g in groups if g.kind == "firmware" and "UDRULT" in g.products]
        assert [r.version for r in switch.releases] == ["7.2.5", "7.2.1", "7.1.26"]
        # One row per model, so each model's verdicts find their release.
        assert len(switch.all_rows) == 6
        os_group = next(g for g in groups if g.kind == "os")
        assert [r.version for r in os_group.releases] == ["4.4.6", "4.3.6"]
        # Scoped: a viewer of no Fabric sees no hardware lines.
        assert all(g.kind == "app" for g in await service.catalog(db, []))


async def test_catalog_page_shows_hardware_lines(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    async with db_session_factory() as db:
        console = Console(host_id="h", name="GW", model="UDRULT", model_name="UCG-Ultra",
                          os_version="4.3.6", fabric_id=fabric_id)  # fmt: skip
        console.apps, console.devices = [], []
        db.add(console)
        db.add(LatestRelease(kind="os", product="UDRULT", current_version="4.3.6",
                             version="4.4.6", title="UniFi OS - Cloud Gateways",
                             checked_at=datetime.now(UTC)))  # fmt: skip
        db.add(Release(kind="os", product="UDRULT", version="4.3.6", risk_level=1,
                       title="UniFi OS - Cloud Gateways", community_release_id="c1"))  # fmt: skip
        await db.commit()
    page = await client.get("/releases")
    assert page.status_code == 200
    assert "UniFi OS - Cloud Gateways" in page.text and "UCG-Ultra" in page.text
    assert "Device firmware" not in page.text  # no firmware lines here


# --- Fabric page: running-version risk and auto-update ---------------------------------


async def test_fabric_page_flags_installed_risk_and_auto_update(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    async with db_session_factory() as db:
        console = _nvr(_camera("UVC-G4-DOORBELL", "G4 Doorbell Pro"))
        console.fabric_id = fabric_id
        console.auto_update_schedule = "weekly, day 0, 00:00"
        console.auto_update_include_apps = True
        db.add(console)
        db.add(_protect(reason="doorbells reboot"))  # pending 5.2.0: STOP here
        db.add(Release(kind="os", product="UNVR", version="4.1.13", risk_level=5,
                       reason="disks drop"))  # fmt: skip
        await db.commit()
    page = await client.get(f"/fabrics/{fabric_id}")
    assert "running version at risk" in page.text
    assert "auto-update" in page.text and "doorbells reboot" in page.text


# --- the Status page and job tracking --------------------------------------------------


async def test_tracked_records_success_and_failure(jobs_db: Any) -> None:
    async def ok() -> dict[str, Any]:
        return {"ok": True, "catalog": 20, "queued": 3}

    async def boom() -> None:
        raise RuntimeError("community.ui.com down")

    assert (await job_runs.tracked("refresh-release-catalog", ok()))["catalog"] == 20
    with pytest.raises(RuntimeError):
        await job_runs.tracked("reassess-releases", boom())
    async with jobs_db() as db:
        good = await db.get(JobRun, "refresh-release-catalog")
        bad = await db.get(JobRun, "reassess-releases")
        assert good is not None and good.last_ok and good.last_result == "catalog 20, queued 3"
        assert bad is not None and bad.last_ok is False
        assert "community.ui.com down" in (bad.last_error or "")
    assert job_runs.summarize({"ok": False, "error": "x"}) == "error x"
    assert job_runs.summarize(None) is None and job_runs.summarize(4) == "4"


def test_next_run_estimates() -> None:
    from app.web.routes.status import next_run

    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    every = timedelta(hours=24)
    assert next_run(None, now - timedelta(hours=1), every, now) == now + timedelta(hours=23)
    assert next_run(None, None, every, now) is None
    daily = next_run(crontab(hour=3, minute=0), now - timedelta(hours=9), None, now)
    assert daily is not None and daily > now


async def test_status_page(
    client: Any, db_session_factory: Any, login_as: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web.routes import status as status_routes

    async def _workers() -> int:
        return 1

    monkeypatch.setattr(status_routes, "workers_online", _workers)
    async with db_session_factory() as db:
        db.add(JobRun(name="refresh-release-catalog", last_started_at=datetime.now(UTC),
                      last_finished_at=datetime.now(UTC), last_ok=False,
                      last_error_at=datetime.now(UTC), last_error="HTTP 503"))  # fmt: skip
        now = datetime.now(UTC)
        db.add(Release(kind="app", product="network", version="1.0.0",
                       assessment_error="Rate limited", assessment_queued_at=now))  # fmt: skip
        db.add(Release(kind="app", product="network", version="1.0.1", assessment_queued_at=now))
        db.add(Release(kind="app", product="network", version="1.0.2", stage="RC",
                       assessment_queued_at=now))  # fmt: skip
        db.add(Release(kind="app", product="network", version="1.0.3",
                       assessment_queued_at=now - timedelta(hours=2)))  # fmt: skip
        (await get_or_create_app_settings(db)).hidden_release_stages = ["RC"]
        await db.commit()
    page = await client.get("/status")
    assert page.status_code == 200
    assert "Releases catalogue" in page.text and "HTTP 503" in page.text
    assert "Rate limited" in page.text and "1 online" in page.text
    assert "AI slots 0/2 in use" in page.text
    # Only what really waits is "queued"; a failed or lost one is retried,
    # a hidden stage isn't rated on purpose.
    assert ">1 queued<" in page.text and "2 failed, retried within the hour" in page.text
    assert "1 in a hidden stage" in page.text
    assert 'href="/status"' in (await client.get("/dashboard")).text

    await login_as(client, permissions={Permission.FABRIC_VIEW}, username="viewer")
    assert (await client.get("/status")).status_code == 403
