"""0.4.0: settings per Fabric/console/device (`app.releases.policy`), hidden
release stages, the per-Fabric sync interval, "Assess all", the primary/
fallback AI model with per-provider timeouts, and the per-call accounting
(audit log + Settings → AI usage)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.ai.base import AiProviderError, ChatTurnResult
from app.core.app_settings import get_or_create_app_settings
from app.core.security import encrypt_secret
from app.db.models.ai_conversation import AiConversation
from app.db.models.ai_message import AiMessage, AiMessageRole
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig, AiProviderKind
from app.db.models.ai_usage import AiUsageRecord
from app.db.models.app_settings import AppSettings
from app.db.models.audit_log import AuditLogEntry
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.release import Release
from app.releases.evidence import CommunityRelease, Evidence
from app.releases.policy import (
    Level,
    clean_stages,
    console_policy,
    device_policy,
    fabric_policy,
    stages_from_form,
)
from app.releases.verdicts import (
    TargetSource,
    Verdict,
    installed_targets,
    policy_for,
    update_targets,
    verdicts,
)
from app.tasks import ai_jobs, jobs, release_jobs
from tests.test_ai_web import FakeClient, get_user, text_turn
from tests.test_ai_web import use_test_db as use_test_db  # noqa: F401 (fixture)
from tests.test_releases import _configure_model, _console, _fabric_with_inventory, _FakeClient
from tests.test_releases import jobs_db as jobs_db  # noqa: F401 (fixture)
from tests.test_releases import no_community as no_community  # noqa: F401 (fixture)
from tests.test_releases import published as published  # noqa: F401 (fixture)

# --- resolution ----------------------------------------------------------------------


def test_each_setting_inherits_on_its_own() -> None:
    settings = AppSettings(max_acceptable_risk_level=2, hidden_release_stages=["rc", "X"])
    fabric = Fabric(name="F", max_acceptable_risk_level=3)
    console = Console(host_id="h", max_acceptable_risk_level=4)
    device = Device(mac="m", hidden_release_stages=[])

    assert clean_stages(["rc", "X", "GA"]) == ["GA", "RC"]
    at_fabric = fabric_policy(fabric, settings)
    assert (at_fabric.max_risk_level, at_fabric.risk_from) == (3, Level.FABRIC)
    assert at_fabric.hidden_stages == {"RC"} and at_fabric.stages_from is Level.GLOBAL
    at_console = console_policy(console, fabric, settings)
    assert (at_console.max_risk_level, at_console.risk_from) == (4, Level.CONSOLE)
    at_device = device_policy(device, fabric, settings)
    # Its own (empty) stage list wins; the level still comes from the Fabric,
    # not from the console it is adopted on.
    assert at_device.hidden_stages == frozenset() and at_device.stages_from is Level.DEVICE
    assert (at_device.max_risk_level, at_device.risk_from) == (3, Level.FABRIC)
    assert at_device.hides(None) is False and at_fabric.hides(" rc ") is True

    assert stages_from_form({"stages_mode": "inherit", "hide_stage_RC": "1"}) is None
    assert stages_from_form({"stages_mode": "own", "hide_stage_T": "1"}) == ["T"]
    assert stages_from_form({"hide_stage_U": "1"}, inherit_allowed=False) == ["U"]


def test_verdicts_use_the_device_level_and_leave_hidden_stages_out() -> None:
    console = _console()
    camera = console.devices[0]
    camera.max_acceptable_risk_level = 3
    settings = AppSettings(max_acceptable_risk_level=2, hidden_release_stages=["RC"])
    releases = {
        ("app", "protect", "7.3.74"): Release(
            kind="app", product="protect", version="7.3.74", stage="RC", risk_level=1
        ),
        ("firmware", "G5 Flex", "5.5.51"): Release(
            kind="firmware", product="G5 Flex", version="5.5.51", stage="GA", risk_level=3
        ),
    }
    resolve = policy_for(None, settings)
    targets = update_targets([console], {})
    results = {v.target.product: v for v in verdicts(targets, releases, resolve)}
    # The RC update to Protect is hidden; the camera's own level makes 3 GO.
    assert "protect" not in results
    assert results["G5 Flex"].verdict is Verdict.GO and results["G5 Flex"].max_risk_level == 3
    # What already runs is never hidden.
    releases[("app", "protect", "7.3.70")] = Release(
        kind="app", product="protect", version="7.3.70", stage="RC", risk_level=1
    )
    running = verdicts(installed_targets([console]), releases, resolve)
    assert any(
        v.target.product == "protect" and v.target.source is TargetSource.INSTALLED
        for v in running
    )


# --- web: console/device/Fabric settings ---------------------------------------------


async def _console_in(factory: Any, fabric_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as db:
        console = _console()
        console.fabric_id = fabric_id
        db.add(console)
        await db.commit()
        return console.id, console.devices[0].id


async def test_console_and_device_settings_pages(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    console_id, device_id = await _console_in(db_session_factory, fabric_id)

    page = await client.get(f"/fabrics/{fabric_id}/consoles/{console_id}/settings")
    assert page.status_code == 200 and "Inherit (2/5" in page.text
    csrf = client.cookies.get("csrftoken")
    saved = await client.post(
        f"/fabrics/{fabric_id}/consoles/{console_id}/settings",
        data={
            "max_acceptable_risk_level": "4",
            "stages_mode": "own",
            "hide_stage_RC": "1",
            "csrf_token": csrf,
        },
    )
    assert saved.status_code == 303
    bad = await client.post(
        f"/fabrics/{fabric_id}/devices/{device_id}/settings",
        data={"max_acceptable_risk_level": "9", "csrf_token": csrf},
    )
    assert bad.status_code == 422
    device_saved = await client.post(
        f"/fabrics/{fabric_id}/devices/{device_id}/settings",
        data={"max_acceptable_risk_level": "1", "stages_mode": "inherit", "csrf_token": csrf},
    )
    assert device_saved.status_code == 303
    # Not this Fabric's device: 404, as for anything out of scope.
    other = uuid.uuid4()
    assert (await client.get(f"/fabrics/{other}/devices/{device_id}/settings")).status_code == 404

    async with db_session_factory() as db:
        console = await db.get(Console, console_id)
        device = await db.get(Device, device_id)
        assert console is not None and device is not None
        assert console.max_acceptable_risk_level == 4
        assert console.hidden_release_stages == ["RC"]
        assert device.max_acceptable_risk_level == 1 and device.hidden_release_stages is None
        actions = {
            e.action for e in (await db.execute(select(AuditLogEntry))).scalars()
        }
        assert {"console.settings.update", "device.settings.update"} <= actions
    inventory = await client.get(f"/fabrics/{fabric_id}/inventory-panel")
    assert inventory.status_code == 200
    assert "own: 4/5" in inventory.text and "own: 1/5" in inventory.text


async def test_fabric_form_saves_stages_and_sync_interval(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    await client.get(f"/fabrics/{fabric_id}/edit")
    csrf = client.cookies.get("csrftoken")
    response = await client.post(
        f"/fabrics/{fabric_id}/edit",
        data={
            "name": "Office",
            "is_enabled": "1",
            "stages_mode": "own",
            "hide_stage_T": "1",
            "hide_stage_U": "1",
            "inventory_sync_interval_seconds": "300",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 303
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        assert fabric.hidden_release_stages == ["T", "U"]
        assert fabric.inventory_sync_interval_seconds == 300
    too_fast = await client.post(
        f"/fabrics/{fabric_id}/edit",
        data={"name": "Office", "inventory_sync_interval_seconds": "5", "csrf_token": csrf},
    )
    assert too_fast.status_code == 422


async def test_global_hidden_stages_leave_the_catalog(
    client: Any, db_session_factory: Any
) -> None:
    async with db_session_factory() as db:
        for version, stage in (("7.3.74", "GA"), ("7.4.2", "RC")):
            db.add(
                Release(
                    kind="app", product="protect", version=version, stage=stage,
                    title="UniFi Protect Application", community_release_id=f"c-{version}",
                )
            )  # fmt: skip
        await db.commit()
    assert "7.4.2" in (await client.get("/releases")).text
    await client.get("/settings?tab=releases")
    csrf = client.cookies.get("csrftoken")
    saved = await client.post(
        "/settings/releases",
        data={"max_acceptable_risk_level": "2", "hide_stage_RC": "1", "csrf_token": csrf},
    )
    assert saved.status_code == 303
    async with db_session_factory() as db:
        assert (await get_or_create_app_settings(db)).hidden_release_stages == ["RC"]
    page = (await client.get("/releases")).text
    assert "7.3.74" in page and "7.4.2" not in page
    # The stage badges explain themselves on hover.
    assert 'title="GA — General Availability' in page


# --- sync interval -------------------------------------------------------------------


async def test_sync_tick_enqueues_only_due_fabrics(
    jobs_db: Any,  # noqa: F811
    celery_calls: Any,
) -> None:
    now = datetime.now(UTC)
    async with jobs_db() as db:
        quick = Fabric(
            name="quick", api_key_encrypted=encrypt_secret("k"),
            inventory_sync_interval_seconds=300, last_sync_attempt_at=now - timedelta(minutes=2),
        )  # fmt: skip
        fresh = Fabric(name="fresh", api_key_encrypted=encrypt_secret("k"))
        off = Fabric(name="off", api_key_encrypted=encrypt_secret("k"), is_enabled=False)
        db.add_all([quick, fresh, off])
        await db.commit()
    assert await jobs._sync_all_fabrics(now) == 1  # only "fresh", never synced
    assert await jobs._sync_all_fabrics(now + timedelta(minutes=1)) == 0
    # Five minutes after "quick" was last enqueued; "fresh" waits its 15.
    assert await jobs._sync_all_fabrics(now + timedelta(minutes=3)) == 1
    assert celery_calls.names.count("app.tasks.jobs.sync_fabric") == 2


# --- assessment: fallback, accounting, hidden stages ---------------------------------


class _Failing(_FakeClient):
    async def send(self, *args: Any, **kwargs: Any) -> ChatTurnResult:
        raise AiProviderError("Anthropic request failed: timed out")


async def _add_fallback(factory: Any) -> None:
    async with factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.OPENAI, enabled=True, api_key_encrypted=encrypt_secret("k")
        )
        db.add(provider)
        await db.flush()
        model = AiModel(provider_id=provider.id, model_id="backup-model", enabled=True)
        db.add(model)
        await db.flush()
        (await get_or_create_app_settings(db)).ai_fallback_model_id = model.id
        await db.commit()


def _evidence(stage: str) -> Any:
    async def gather(app_settings: Any, release: Release) -> Evidence:
        return Evidence(
            release=CommunityRelease(
                id="p1", title="UniFi Protect Application", version=release.version,
                stage=stage, url="https://community.ui.com/releases/p/p1",
                created_at="2026-11-01T00:00:00Z", notes="",
            )
        )  # fmt: skip

    return gather


async def _protect_release(factory: Any) -> Release:
    async with factory() as db:
        release: Release = (
            await db.execute(
                select(Release).where(Release.product == "protect", Release.version == "7.3.74")
            )
        ).scalar_one()
        return release


async def test_assessment_falls_back_and_is_accounted_for(
    jobs_db: Any,  # noqa: F811
    no_community: Any,  # noqa: F811
    published: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _configure_model(jobs_db)
    await _add_fallback(jobs_db)
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))
    good = _FakeClient('{"risk_level": 2, "reason": "fine", "summary": "Fine."}')
    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence("GA"))
    monkeypatch.setattr(
        release_jobs,
        "build_client",
        lambda p: _Failing("") if p.kind is AiProviderKind.ANTHROPIC else good,
    )
    release = await _protect_release(jobs_db)

    result = await release_jobs._assess_release(str(release.id))
    assert result["ok"] is True

    async with jobs_db() as db:
        stored = await db.get(Release, release.id)
        assert stored is not None
        assert stored.assessed_by_model == "openai/backup-model"
        assert stored.assessment_tokens == 120 and stored.assessment_duration_ms is not None
        usage = (await db.execute(select(AiUsageRecord))).scalars().all()
        assert [(u.purpose, u.fallback, u.model_id) for u in usage] == [
            ("assessment", True, "backup-model")
        ]
        assert usage[0].label == "UniFi Protect Application 7.3.74"
        entry = (
            await db.execute(select(AuditLogEntry).where(AuditLogEntry.action == "release.assess"))
        ).scalar_one()
        assert entry.details is not None
        assert entry.details["fallback"] is True and entry.details["tokens"] == 120
        assert "timed out" in entry.details["failed_first"][0]
        assert "120 tokens" in entry.summary


async def test_both_models_failing_is_audited_as_a_failure(
    jobs_db: Any,  # noqa: F811
    no_community: Any,  # noqa: F811
    published: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _configure_model(jobs_db)
    await _add_fallback(jobs_db)
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))
    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence("GA"))
    # An answer that can't be used still cost tokens — they're recorded.
    monkeypatch.setattr(
        release_jobs,
        "build_client",
        lambda p: _Failing("") if p.kind is AiProviderKind.ANTHROPIC else _FakeClient("no"),
    )
    release = await _protect_release(jobs_db)
    result = await release_jobs._assess_release(str(release.id))
    assert result["ok"] is False and "anthropic/fake-model" in result["error"]
    async with jobs_db() as db:
        entry = (
            await db.execute(select(AuditLogEntry).where(AuditLogEntry.action == "release.assess"))
        ).scalar_one()
        assert entry.outcome.value == "failure" and entry.details is not None
        assert entry.details["tokens"] == 120


async def test_a_hidden_stage_is_not_rated_unless_asked(
    jobs_db: Any,  # noqa: F811
    no_community: Any,  # noqa: F811
    published: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _configure_model(jobs_db)
    async with jobs_db() as db:
        (await get_or_create_app_settings(db)).hidden_release_stages = ["RC"]
        await db.commit()
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))
    fake = _FakeClient('{"risk_level": 2, "reason": "fine", "summary": "Fine."}')
    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence("RC"))
    monkeypatch.setattr(release_jobs, "build_client", lambda p: fake)
    release = await _protect_release(jobs_db)

    skipped = await release_jobs._assess_release(str(release.id))
    assert skipped == {"ok": True, "skipped": "stage RC is hidden"} and fake.prompts == []
    async with jobs_db() as db:
        stored = await db.get(Release, release.id)
        assert stored is not None and stored.stage == "RC" and stored.risk_level is None

    # This Fabric shows RC: now it is worth rating.
    async with jobs_db() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        fabric.hidden_release_stages = []
        await db.commit()
    assert (await release_jobs._assess_release(str(release.id)))["ok"] is True
    assert len(fake.prompts) == 1
    # And a person asking for it always gets a rating.
    async with jobs_db() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        fabric.hidden_release_stages = None
        await db.commit()
    assert "skipped" not in await release_jobs._assess_release(str(release.id), True)


# --- web: Assessments ----------------------------------------------------------------


async def test_assessments_list_the_waiting_devices_and_assess_all(
    client: Any, db_session_factory: Any, accept_any_api_key: Any, celery_calls: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    await _configure_model(db_session_factory)
    fabric_id = await create_fabric_via_web(client, "Office")
    await _console_in(db_session_factory, fabric_id)
    async with db_session_factory() as db:
        db.add(Release(kind="app", product="protect", version="7.3.74", risk_level=4))
        db.add(Release(kind="firmware", product="G5 Flex", version="5.5.51", risk_level=1))
        await db.commit()

    page = await client.get("/assessments")
    assert page.status_code == 200
    assert "HQ — Protect" in page.text and "Waiting for it" in page.text
    assert 'action="/assessments/assess-all"' in page.text
    celery_calls.clear()
    response = await client.post(
        "/assessments/assess-all", data={"csrf_token": client.cookies.get("csrftoken")}
    )
    assert response.status_code == 303 and "queued=2" in response.headers["location"]
    assert celery_calls.names.count("app.tasks.release_jobs.assess_release") == 2
    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "release.assess.request_all")
            )
        ).scalar_one()
        assert entry.details == {"releases": 2}


# --- Settings → AI ---------------------------------------------------------------------


async def test_model_choice_timeouts_and_the_usage_counter(
    client: Any, db_session_factory: Any
) -> None:
    await _configure_model(db_session_factory)
    async with db_session_factory() as db:
        primary = (await db.execute(select(AiModel))).scalar_one()
        provider = AiProviderConfig(
            kind=AiProviderKind.OPENAI, enabled=True, api_key_encrypted=encrypt_secret("k")
        )
        db.add(provider)
        await db.flush()
        backup = AiModel(provider_id=provider.id, model_id="backup-model", enabled=True)
        db.add(backup)
        db.add(
            AiUsageRecord(
                provider_kind=AiProviderKind.ANTHROPIC, model_id="fake-model",
                input_tokens=1200, output_tokens=345, purpose="assessment",
                label="UniFi Protect 7.3.74", duration_ms=8400, fallback=True,
            )
        )  # fmt: skip
        await db.commit()
        primary_id, backup_id = primary.id, backup.id

    await client.get("/settings?tab=ai")
    csrf = client.cookies.get("csrftoken")
    same = await client.post(
        "/settings/ai/model-choice",
        data={"primary_model_id": str(primary_id), "fallback_model_id": str(primary_id),
              "csrf_token": csrf},
    )  # fmt: skip
    assert "must differ" in same.text
    ok = await client.post(
        "/settings/ai/model-choice",
        data={"primary_model_id": str(primary_id), "fallback_model_id": str(backup_id),
              "csrf_token": csrf},
    )  # fmt: skip
    assert ok.status_code == 303
    slow = await client.post(
        "/settings/ai/provider",
        data={"kind": "openai", "enabled": "1", "timeout_seconds": "1000", "csrf_token": csrf},
    )
    assert "between 5 and 300" in slow.text
    fine = await client.post(
        "/settings/ai/provider",
        data={"kind": "openai", "enabled": "1", "timeout_seconds": "120", "csrf_token": csrf},
    )
    assert fine.status_code == 303
    async with db_session_factory() as db:
        app_settings = await get_or_create_app_settings(db)
        assert app_settings.release_assessment_model_id == primary_id
        assert app_settings.ai_fallback_model_id == backup_id
        stored = (
            await db.execute(
                select(AiProviderConfig).where(AiProviderConfig.kind == AiProviderKind.OPENAI)
            )
        ).scalar_one()
        assert stored.timeout_seconds == 120

    page = (await client.get("/settings?tab=ai")).text
    assert "AI usage" in page and "1 545" in page
    assert "UniFi Protect 7.3.74" in page and "8.4 s" in page and ">fallback<" in page


# --- assistant: fallback -------------------------------------------------------------


async def test_assistant_falls_back_with_a_plain_transcript(
    client: Any,
    db_session_factory: Any,
    use_test_db: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await get_user(db_session_factory)
    await _configure_model(db_session_factory)
    await _add_fallback(db_session_factory)
    async with db_session_factory() as db:
        anthropic = (
            await db.execute(
                select(AiProviderConfig).where(AiProviderConfig.kind == AiProviderKind.ANTHROPIC)
            )
        ).scalar_one()
        conversation = AiConversation(
            user_id=user.id, title="Updates", provider_id=anthropic.id, model_id="fake-model"
        )
        db.add(conversation)
        await db.flush()
        db.add_all(
            [
                AiMessage(
                    conversation_id=conversation.id, role=AiMessageRole.USER,
                    content="earlier question", provider_native=[{"role": "user"}],
                    created_at=datetime.now(UTC) - timedelta(minutes=2),
                ),
                AiMessage(
                    conversation_id=conversation.id, role=AiMessageRole.ASSISTANT,
                    content="earlier answer", provider_native=[{"role": "assistant"}],
                    created_at=datetime.now(UTC) - timedelta(minutes=1),
                ),
            ]
        )  # fmt: skip
        await db.commit()
        conversation_id = conversation.id

    class _Down(FakeClient):
        async def send(self, *args: Any, **kwargs: Any) -> ChatTurnResult:
            raise AiProviderError("Anthropic returned HTTP 529: overloaded")

    backup = FakeClient([text_turn("Here you go.")])
    monkeypatch.setattr(
        ai_jobs,
        "build_client",
        lambda p: _Down([]) if p.kind is AiProviderKind.ANTHROPIC else backup,
    )
    result = await ai_jobs._run_turn(str(conversation_id), "and now?", allow_tools=False)
    assert result["ok"] is True

    sent = backup.calls[0]["messages"]
    assert len(sent) == 1 and "different AI model" in sent[0]["content"]
    assert "earlier answer" in sent[0]["content"] and "and now?" in sent[0]["content"]
    async with db_session_factory() as db:
        reply = (
            await db.execute(
                select(AiMessage)
                .where(AiMessage.conversation_id == conversation_id)
                .order_by(AiMessage.created_at.desc())
            )
        ).scalars().first()
        assert reply is not None and reply.provider_kind is AiProviderKind.OPENAI
        entry = (
            await db.execute(select(AuditLogEntry).where(AuditLogEntry.action == "ai.turn"))
        ).scalar_one()
        assert entry.details is not None and entry.details["fallback"] is True
        assert entry.details["model"] == "openai/backup-model" and entry.details["tokens"] == 15
        usage = (await db.execute(select(AiUsageRecord))).scalars().all()
        assert [(u.purpose, u.fallback) for u in usage] == [("assistant", True)]


# --- 0.4.1: catalogue keeps three, console entries, layout ---------------------------


async def test_hidden_stages_still_leave_three_versions_in_the_catalog(
    jobs_db: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    celery_calls: Any,
) -> None:
    from app.releases import service
    from app.releases.sources.community import CommunityClient

    async def _search(self: Any, term: str, limit: int = 25) -> list[Any]:
        if term != "UniFi Network Application":
            return []
        return [
            {"id": f"n{n}", "slug": "s", "title": "UniFi Network Application",
             "version": f"11.0.{n}", "stage": stage, "createdAt": "2026-11-01T00:00:00Z"}
            for n, stage in ((110, "RC"), (108, "RC"), (104, "GA"), (103, "GA"), (99, "GA"))
        ]  # fmt: skip

    monkeypatch.setattr(CommunityClient, "search", _search)
    async with jobs_db() as db:
        (await get_or_create_app_settings(db)).hidden_release_stages = ["RC"]
        await db.commit()
    await release_jobs._refresh_release_catalog()
    async with jobs_db() as db:
        shown = await service.catalog(db, hidden_stages=frozenset({"RC"}))
        network = next(g for g in shown if g.products == ["network"])
        assert [r.version for r in network.releases] == ["11.0.104", "11.0.103", "11.0.99"]


def test_a_consoles_own_device_entry_is_not_a_second_update() -> None:
    console = Console(host_id="h", name="NVR", model="ENVR", os_version="6.0.11",
                      os_update_available="6.0.17")  # fmt: skip
    console.apps = []
    console.devices = [Device(id=uuid.uuid4(), mac="m", model="ENVR", version="6.0.11",
                              update_available="6.0.17")]  # fmt: skip
    targets = update_targets([console], {})
    assert [(t.kind, t.product) for t in targets] == [("os", "ENVR")]


async def test_wide_tables_scroll_in_their_own_container(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    await _console_in(db_session_factory, fabric_id)
    for url in ("/assessments", "/releases", "/audit", f"/fabrics/{fabric_id}/inventory-panel"):
        page = (await client.get(url)).text
        assert page.count("<table") == page.count('<div class="table-scroll">'), url
    assert ".table-scroll" in (await client.get("/static/css/style.css")).text


async def test_an_assessment_without_a_free_slot_comes_back_later(
    jobs_db: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    celery_calls: Any,
) -> None:
    from app.ai import slots

    limits: list[int] = []

    async def _full(limit: int, ttl: int) -> None:
        limits.append(limit)
        return None

    monkeypatch.setattr(slots, "acquire", _full)
    async with jobs_db() as db:
        (await get_or_create_app_settings(db)).ai_max_concurrent_assessments = 3
        await db.commit()
    result = await release_jobs._assess_release(str(uuid.uuid4()), True)
    assert result["ok"] is True and 30 <= result["deferred"] <= 60
    assert limits == [3]
    assert celery_calls.names == ["app.tasks.release_jobs.assess_release"]


async def test_a_slot_is_released_after_the_assessment(
    jobs_db: Any,  # noqa: F811
    ai_slots_free: list[str],
) -> None:
    assert (await release_jobs._assess_release(str(uuid.uuid4())))["ok"] is False
    assert ai_slots_free == ["test-slot"]
