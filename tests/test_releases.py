"""Release assessment (`app.releases`, `app.tasks.release_jobs`,
`app/web/routes/releases.py`): parsing every source, the AI answer, the
GO/STOP verdicts, the jobs, and the web pages with their permissions.

Nothing here touches the network: community.ui.com, Reddit, web search and
the AI provider are all replaced by fakes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from app.ai.base import BaseAiClient, ChatTurnResult, ModelInfo, ToolCall, ToolDefinition
from app.core.app_settings import get_or_create_app_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig, AiProviderKind
from app.db.models.ai_usage import AiUsageRecord
from app.db.models.audit_log import AuditLogEntry
from app.db.models.console import Console, ConsoleApp
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.release import LatestRelease, Release
from app.db.models.role import Permission
from app.releases import assess as assess_module
from app.releases import products
from app.releases.assess import AssessmentError, build_prompt, parse_answer
from app.releases.evidence import CommunityRelease, Evidence, EvidenceItem
from app.releases.sources import community, reddit, web
from app.releases.sources.community import CommunityClient, ReleasePost
from app.releases.verdicts import (
    TargetSource,
    Verdict,
    installed_keys,
    update_targets,
    verdict_for,
    verdicts,
)
from app.releases.versions import normalize_version
from app.tasks import release_jobs

# --- versions and product matching --------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UVC.SAV539g.v5.5.45.67.0dda191.261027.0534", "5.5.45"),
        ("1.4", "1.4"),
        ("v1.4 build", "1.4"),
        ("1.2..3", None),
        (".5.5.51", None),
        ("9" * 100000, None),
        ("9." * 50000, "9.9.9"),
        ("5.5.51", "5.5.51"),
        ("6.0.17+701d2b2", "6.0.17"),
        ("10.6.111", "10.6.111"),
        ("7.6.11.17272", "7.6.11"),
        ("UPV.qcs605.v1.4.33.0.4698daf26.260416.1114", "1.4.33"),
        ("", None),
        (None, None),
        ("unknown", None),
    ],
)
def test_normalize_version(raw: str | None, expected: str | None) -> None:
    assert normalize_version(raw) == expected


@pytest.mark.parametrize(
    ("kind", "product", "title", "expected"),
    [
        ("app", "network", "UniFi Network Application", True),
        ("app", "protect", "UniFi Protect Application", True),
        ("app", "protect", "UniFi Protect iOS", False),
        ("app", "protect", "UniFi Protect Cameras", False),
        ("app", "innerspace", "UniFi InnerSpace", True),
        ("os", "UNVR", "UniFi OS - Network Video Recorders", True),
        ("os", "UNVR", "UniFi OS - Enterprise Network Video Recorders", False),
        ("os", "ENVR", "UniFi OS - Enterprise Network Video Recorders", True),
        ("os", "UDRULT", "UniFi OS - Cloud Gateways", True),
        ("os", "UDRULT", "UniFi OS - Dream Routers", False),
        ("os", "UDM-Pro", "UniFi OS - Dream Machines", True),
        ("firmware", "G5 Flex", "UniFi Protect Cameras", True),
        ("firmware", "G5 Flex", "UniFi Protect Application", False),
        ("firmware", "US48PRO", "UniFi Switch", True),
        ("firmware", "U6-Pro", "UniFi Access Point for U6-LR, U6-Lite, U6+ & more", True),
        ("firmware", "U6-Pro", "UniFi OS - Dream Machines", False),
        ("firmware", "U6-Pro", "Security Advisory Bulletin", False),
    ],
)
def test_title_matches(kind: str, product: str, title: str, expected: bool) -> None:
    assert products.title_matches(kind, product, title) is expected


def test_is_newer_compares_numerically() -> None:
    assert products.is_newer("11.0.104", "10.6.111")
    assert products.is_newer("5.5.51", "UVC.SAV539g.v5.5.45.67")
    assert not products.is_newer("10.6.111", "10.6.111")
    assert not products.is_newer("9.9.9", "10.0.1")
    assert not products.is_newer(None, "1.0.0")


def test_community_search_url_is_on_community_ui_com() -> None:
    url = products.community_search_url("app", "network", "11.0.104")
    assert url.startswith("https://community.ui.com/releases?q=")
    assert "Network" in url and "11.0.104" in url


# --- community.ui.com ---------------------------------------------------------

_SEARCH_ITEMS = [
    {"id": "r1", "slug": "UniFi-Protect-iOS-3-15-0", "title": "UniFi Protect iOS",
     "version": "3.15.0", "stage": "GA"},
    {"id": "r2", "slug": "UniFi-Network-Application-11-0-104",
     "title": "UniFi Network Application", "version": "11.0.104", "stage": "GA",
     "createdAt": "2026-11-20T14:02:27.341Z"},
    {"id": "r3", "slug": "x", "title": "UniFi Network Application", "version": "11.1.1",
     "stage": "RC"},
    {"id": "r4", "slug": "y", "title": "UniFi Network Application", "version": "10.6.111",
     "stage": "GA"},
    "garbage",
    {"id": None, "title": "UniFi Network Application", "version": "1.0.0"},
]  # fmt: skip


def test_pick_release_matches_exact_version_and_product() -> None:
    post = community.pick_release("app", "network", "11.0.104", _SEARCH_ITEMS)
    assert post is not None
    assert post.id == "r2"
    assert post.url == "https://community.ui.com/releases/UniFi-Network-Application-11-0-104/r2"
    assert community.pick_release("app", "protect", "11.0.104", _SEARCH_ITEMS) is None


def test_pick_latest_takes_the_newest_ga_only() -> None:
    post = community.pick_latest("app", "network", _SEARCH_ITEMS)
    assert post is not None and post.version == "11.0.104"


def test_parse_release_and_questions_fill_evidence() -> None:
    evidence = Evidence()
    community.parse_release(
        evidence,
        {
            "title": "UniFi Network Application",
            "version": "11.0.104",
            "slug": "UniFi-Network-Application-11-0-104",
            "stage": "GA",
            "createdAt": "2026-11-20T14:02:27.341Z",
            "knownIssues": [{"type": "TEXT", "content": "<ul><li>Adoption fails</li></ul>"}],
            "bugfixes": "not a list",
            "comments": {
                "items": [
                    {"stats": {"score": 17}, "content": [{"content": "<p>Broke my VLANs</p>"}]},
                    {"stats": {"score": 2}, "content": []},
                    "garbage",
                ]
            },
        },
        "r2",
    )
    community.parse_questions(
        evidence,
        {"questions": {"items": [
            {"id": "q1", "slug": "s", "title": "APs offline after 11.0.104",
             "stats": {"score": 4, "answers": 3, "views": 99}},
            {"id": "q2"},
        ]}},
        "11.0.104",
    )  # fmt: skip
    assert evidence.release is not None
    assert "Known issues:\nAdoption fails" in evidence.release.notes
    texts = [item.text for item in evidence.items]
    assert "Broke my VLANs" in texts[0]
    assert evidence.items[0].score == 17
    assert any("APs offline" in t for t in texts)
    assert len(evidence.items) == 2
    assert evidence.sources()[0]["url"] == evidence.release.url


async def test_community_client_surfaces_http_errors_as_source_errors() -> None:
    from app.releases.evidence import SourceError

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "community.svc.ui.com"
        return httpx2.Response(503)

    client = CommunityClient(http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(SourceError, match="HTTP 503"):
        await client.find_release("app", "network", "11.0.104")
    await client.aclose()


async def test_community_client_latest_on_the_firmware_line() -> None:
    """Firmware: the newest post with the same title as the installed
    version's post — never a guess when the installed version isn't found."""
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        term = json.loads(request.content)["variables"]["searchTerm"]
        calls.append(term)
        items = [
            {"id": "a", "slug": "s", "title": "UniFi Switch", "version": "7.6.11", "stage": "GA"},
            {"id": "b", "slug": "s", "title": "UniFi Switch", "version": "7.7.3", "stage": "GA"},
            {"id": "c", "slug": "s", "title": "UniFi AirWire and UDB Switch", "version": "8.5.21",
             "stage": "GA"},
        ]  # fmt: skip
        return httpx2.Response(200, json={"data": {"releases": {"items": items}}})

    client = CommunityClient(http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    latest = await client.latest_release("firmware", "US48PRO", "7.6.11")
    assert latest is not None and latest.version == "7.7.3"
    assert await client.latest_release("firmware", "US48PRO", "1.0.0") is None
    await client.aclose()


# --- Reddit and web search ------------------------------------------------------


def test_reddit_parse_posts_and_comments() -> None:
    posts = reddit.parse_posts(
        {
            "data": {
                "children": [
                    {"data": {"id": "p1", "title": "11.0.104 broke DHCP", "selftext": "help",
                              "permalink": "/r/Ubiquiti/comments/p1/x/", "score": 40,
                              "num_comments": 12, "created_utc": 1795000000.0}},
                    {"data": {"id": "p2", "title": "evil", "permalink": "https://evil.example/"}},
                    "garbage",
                ]
            }
        }
    )  # fmt: skip
    assert [p["id"] for p in posts] == ["p1"]
    assert posts[0]["url"] == "https://www.reddit.com/r/Ubiquiti/comments/p1/x/"
    comments = reddit.parse_comments(
        [
            {},
            {"data": {"children": [
                {"kind": "t1", "data": {"body": "Same here", "score": 9}},
                {"kind": "t1", "data": {"body": "[deleted]"}},
                {"kind": "more", "data": {}},
            ]}},
        ]
    )  # fmt: skip
    assert comments == [("Same here", 9)]
    assert reddit.parse_comments({"not": "a list"}) == []


async def test_reddit_client_authenticates_then_searches() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.url.path == "/api/v1/access_token":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx2.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        if "/search" in request.url.path:
            return httpx2.Response(
                200,
                json={"data": {"children": [{"data": {
                    "id": "p1", "title": "Network 11.0.104 issues", "selftext": "",
                    "permalink": "/r/UNIFI/comments/p1/x/", "num_comments": 1}}]}},
            )  # fmt: skip
        return httpx2.Response(
            200, json=[{}, {"data": {"children": [{"kind": "t1", "data": {"body": "yes"}}]}}]
        )

    client = reddit.RedditClient(
        "id", "secret", http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    evidence = Evidence()
    await client.collect(evidence, "UniFi Network Application", "11.0.104")
    await client.aclose()
    assert seen[0] == "POST www.reddit.com/api/v1/access_token"
    assert [i.text for i in evidence.items] == ["Network 11.0.104 issues", "yes"]


async def test_reddit_rejected_credentials_are_a_source_error() -> None:
    from app.releases.evidence import SourceError

    client = reddit.RedditClient(
        "id",
        "bad",
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(401))),
    )
    with pytest.raises(SourceError, match="401"):
        await client.check()
    await client.aclose()


def test_web_parse_results_skips_covered_and_odd_urls() -> None:
    searx = {
        "results": [
            {"title": "Blog: 11.0.104 broke things", "url": "https://blog.example/x",
             "content": "<b>VLAN</b> trouble"},
            {"title": "Reddit", "url": "https://www.reddit.com/r/x"},
            {"title": "Community", "url": "https://community.ui.com/questions/x"},
            {"title": "js", "url": "javascript:alert(1)"},
        ]
    }  # fmt: skip
    assert web.parse_results("searxng", searx) == [
        ("Blog: 11.0.104 broke things", "https://blog.example/x", "VLAN trouble")
    ]
    brave = {"web": {"results": [{"title": "T", "url": "https://x.example", "description": "d"}]}}
    assert web.parse_results("brave", brave) == [("T", "https://x.example", "d")]
    assert web.validate_searxng_url("ftp://x") is not None
    assert web.validate_searxng_url("https://u:p@x") is not None
    assert web.validate_searxng_url("https://search.example") is None


# --- the AI answer ----------------------------------------------------------------


def test_parse_answer_accepts_fences_and_surrounding_text() -> None:
    result = parse_answer(
        'Here you go:\n```json\n{"risk_level": "3", "summary": "Mixed.", '
        '"issues": [{"title": "VLAN", "detail": "drops"}]}\n```'
    )
    assert result.risk_level == 3
    assert result.issues[0].title == "VLAN"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "{not json}",
        '{"risk_level": 9, "summary": "x"}',
        '{"risk_level": 0, "summary": "x"}',
        '{"risk_level": 2}',
        '["a list"]',
    ],
)
def test_parse_answer_rejects_bad_answers(text: str) -> None:
    with pytest.raises(AssessmentError):
        parse_answer(text)


def test_parse_answer_reads_and_trims_the_reason() -> None:
    long_reason = "STP broken, " * 40
    result = parse_answer(json.dumps({"risk_level": 4, "reason": long_reason, "summary": "s"}))
    assert result.reason.startswith("STP broken, STP broken")
    assert len(result.reason) <= assess_module.MAX_REASON
    assert parse_answer('{"risk_level": 1, "summary": "s"}').reason == ""


def test_system_prompt_names_the_instance_language() -> None:
    assert "in Czech" in assess_module.system_prompt("cs")
    assert "in English" in assess_module.system_prompt("xx")


def test_short_reason_falls_back_to_issue_titles() -> None:
    assert _release(reason="STP broken").short_reason == "STP broken"
    old = _release(issues=[{"title": "VLAN drops"}, {"title": "Adoption loop"}, "junk"])
    assert old.short_reason == "VLAN drops, Adoption loop"
    assert _release().short_reason is None


def test_parse_answer_caps_the_issue_list() -> None:
    answer = {"risk_level": 2, "summary": "s", "issues": [{"title": f"i{n}"} for n in range(20)]}
    result = parse_answer(json.dumps(answer))
    assert len(result.issues) == assess_module.MAX_ISSUES


def test_build_prompt_wraps_evidence_and_stays_bounded() -> None:
    evidence = Evidence(
        release=CommunityRelease(
            id="r", title="UniFi Network Application", version="11.0.104", stage="GA",
            url="https://community.ui.com/releases/x/r", created_at=None, notes="Known issues: x",
        )
    )  # fmt: skip
    for n in range(200):
        evidence.add(EvidenceItem(kind="reddit", title=f"t{n}", url=None, text="y" * 2000, score=n))
    evidence.errors.append("Reddit: HTTP 429")
    prompt = build_prompt(
        label="UniFi Network Application", version="11.0.104", stage="GA",
        published_at="2026-11-20", evidence=evidence,
    )  # fmt: skip
    assert prompt.startswith("Release: UniFi Network Application 11.0.104")
    assert "<evidence>" in prompt and "</evidence>" in prompt
    assert len(prompt) < 70000
    # Highest score first: the budget is spent on the most upvoted items.
    assert "t199" in prompt and "] t0 " not in prompt
    assert "Reddit: HTTP 429" in prompt


# --- verdicts ---------------------------------------------------------------------


def _release(**kwargs: Any) -> Release:
    return Release(kind="app", product="network", version="11.0.104", **kwargs)


def test_verdict_for() -> None:
    assert verdict_for(None, 2) is Verdict.PENDING
    assert verdict_for(_release(), 2) is Verdict.PENDING
    assert verdict_for(_release(risk_level=2), 2) is Verdict.GO
    assert verdict_for(_release(risk_level=3), 2) is Verdict.STOP
    assert verdict_for(_release(risk_level=3, manual_risk_level=1), 2) is Verdict.GO
    assert verdict_for(_release(risk_level=1, manual_risk_level=4), 2) is Verdict.STOP
    allowed = _release(risk_level=5, allowed_at=datetime.now(UTC))
    assert verdict_for(allowed, 2) is Verdict.GO


def _console() -> Console:
    console = Console(host_id="h", name="HQ", model="UDM-Pro", os_version="4.1.13")
    console.apps = [
        ConsoleApp(name="network", version="10.6.111", update_available=None),
        ConsoleApp(name="protect", version="7.3.70", update_available="7.3.74"),
    ]
    console.devices = [
        Device(
            id=uuid.uuid4(),
            mac="m1",
            model="G5 Flex",
            version="UVC.SAV530q.v5.5.45.67.0dda191",
            update_available="5.5.51",
        ),
        Device(id=uuid.uuid4(), mac="m2", model="USW-24", firmware_status="updateAvailable"),
        Device(id=uuid.uuid4(), mac="m3", model="U6-Pro", version="6.7.31"),
    ]
    return console


def test_update_targets_combine_site_manager_and_community() -> None:
    console = _console()
    latest = {
        ("app", "network", "10.6.111"): LatestRelease(
            kind="app", product="network", current_version="10.6.111", version="11.0.104",
            checked_at=datetime.now(UTC),
        ),
        ("firmware", "U6-Pro", "6.7.31"): LatestRelease(
            kind="firmware", product="U6-Pro", current_version="6.7.31", version="6.7.31",
            checked_at=datetime.now(UTC),
        ),
    }  # fmt: skip
    targets = update_targets([console], latest)
    by_product = {t.product: t for t in targets}
    assert by_product["network"].target_version == "11.0.104"
    assert by_product["network"].source is TargetSource.COMMUNITY
    assert by_product["protect"].source is TargetSource.SITE_MANAGER
    assert by_product["G5 Flex"].target_version == "5.5.51"
    assert by_product["USW-24"].target_version is None
    # Same version on community.ui.com: nothing to update to.
    assert "U6-Pro" not in by_product
    assert ("firmware", "G5 Flex", "5.5.45") in installed_keys([console])

    results = {v.target.product: v.verdict for v in verdicts(targets, {}, 2)}
    assert results["USW-24"] is Verdict.UNKNOWN
    assert results["protect"] is Verdict.PENDING


# --- jobs ---------------------------------------------------------------------------


@pytest.fixture
def jobs_db(db_session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "AsyncSessionLocal", db_session_factory)
    return db_session_factory


@pytest.fixture
def no_community(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    """community.ui.com answers "nothing found" (and records the lookups)."""
    lookups: list[tuple[str, str, str | None]] = []

    async def _latest(self: Any, kind: str, product: str, current: str | None = None) -> None:
        lookups.append((kind, product, current))
        return None

    async def _find(self: Any, kind: str, product: str, version: str) -> None:
        return None

    monkeypatch.setattr(CommunityClient, "latest_release", _latest)
    monkeypatch.setattr(CommunityClient, "find_release", _find)
    return lookups


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def _publish(fabric_id: str, kind: str) -> None:
        sent.append((fabric_id, kind))

    monkeypatch.setattr(release_jobs, "publish_fabric_event", _publish)
    return sent


async def _fabric_with_inventory(factory: Any, *, max_level: int | None = None) -> uuid.UUID:
    async with factory() as db:
        fabric = Fabric(
            name=f"F-{uuid.uuid4().hex[:6]}",
            api_key_encrypted=encrypt_secret("k"),
            max_acceptable_risk_level=max_level,
        )
        db.add(fabric)
        await db.flush()
        console = _console()
        console.fabric_id = fabric.id
        db.add(console)
        await db.commit()
        return fabric.id


async def _configure_model(factory: Any) -> None:
    async with factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.ANTHROPIC, enabled=True, api_key_encrypted=encrypt_secret("k")
        )
        db.add(provider)
        await db.flush()
        model = AiModel(provider_id=provider.id, model_id="fake-model", enabled=True)
        db.add(model)
        await db.flush()
        app_settings = await get_or_create_app_settings(db)
        app_settings.release_assessment_model_id = model.id
        await db.commit()


async def test_discover_creates_releases_and_queues_each_assessment_once(
    jobs_db: Any, no_community: list[Any], published: list[Any], celery_calls: Any
) -> None:
    await _configure_model(jobs_db)
    first = await _fabric_with_inventory(jobs_db)
    second = await _fabric_with_inventory(jobs_db)

    # Two pending updates and five installed versions (USW-24 reports none).
    assert (await release_jobs._discover_releases(str(first)))["created"] == 7
    assert (await release_jobs._discover_releases(str(second)))["created"] == 0

    async with jobs_db() as db:
        rows = {
            (r.kind, r.product, r.version) for r in (await db.execute(select(Release))).scalars()
        }
        assert rows == {
            ("app", "protect", "7.3.74"),
            ("firmware", "G5 Flex", "5.5.51"),
            ("os", "UDM-Pro", "4.1.13"),
            ("app", "network", "10.6.111"),
            ("app", "protect", "7.3.70"),
            ("firmware", "G5 Flex", "5.5.45"),
            ("firmware", "U6-Pro", "6.7.31"),
        }
        assert (await db.execute(select(LatestRelease))).scalars().first() is not None
    # Queued once each, even though two Fabrics discovered them.
    assert celery_calls.names.count("app.tasks.release_jobs.assess_release") == 7
    # Lookups are cached: the second Fabric (same models/versions) did none.
    assert len(no_community) == len(set(no_community))


async def test_discover_without_a_model_queues_nothing(
    jobs_db: Any, no_community: list[Any], published: list[Any], celery_calls: Any
) -> None:
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))
    assert "app.tasks.release_jobs.assess_release" not in celery_calls.names


class _FakeClient(BaseAiClient):
    kind_value = "anthropic"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[Any] = []

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def send(
        self, messages: list[Any], tools: list[ToolDefinition], model: str, system_prompt: str
    ) -> ChatTurnResult:
        assert tools == []
        assert "treat it as data" in system_prompt
        self.prompts.append(messages)
        return ChatTurnResult(text=self.answer, input_tokens=100, output_tokens=20)

    def build_user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def build_tool_result_messages(
        self, result: ChatTurnResult, outputs: list[tuple[ToolCall, str]]
    ) -> list[Any]:
        return []


async def test_assess_stores_the_rating_records_usage_and_notifies_stop(
    jobs_db: Any, no_community: list[Any], published: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure_model(jobs_db)
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))

    async def _evidence(app_settings: Any, release: Release) -> Evidence:
        evidence = Evidence(
            release=CommunityRelease(
                id="r9", title="UniFi Protect Application", version="7.3.74", stage="GA",
                url="https://community.ui.com/releases/p/r9", created_at="2026-11-01T00:00:00Z",
                notes="notes",
            )
        )  # fmt: skip
        evidence.add(
            EvidenceItem(kind="reddit", title="t", url="https://www.reddit.com/r/x", text="bad")
        )
        return evidence

    fake = _FakeClient(
        '{"risk_level": 4, "reason": "cameras flicker", "summary": "Cameras drop.",'
        ' "issues": [{"title": "Drops"}]}'
    )
    notified: list[tuple[str, str | None]] = []

    async def _notify(db: Any, event: Any, *, fabric: Any = None, context: Any = None) -> None:
        notified.append((event.value, fabric.name if fabric else None))

    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence)
    monkeypatch.setattr(release_jobs, "build_client", lambda provider: fake)
    monkeypatch.setattr(release_jobs, "notify", _notify)

    async with jobs_db() as db:
        release = (
            await db.execute(
                select(Release).where(Release.product == "protect", Release.version == "7.3.74")
            )
        ).scalar_one()
    result = await release_jobs._assess_release(str(release.id))
    assert result == {"ok": True, "risk_level": 4, "shared_with": 0}

    async with jobs_db() as db:
        stored = await db.get(Release, release.id)
        assert stored is not None
        assert stored.risk_level == 4 and stored.summary == "Cameras drop."
        assert stored.reason == "cameras flicker"
        assert stored.community_url == "https://community.ui.com/releases/p/r9"
        assert stored.title == "UniFi Protect Application"
        assert stored.assessed_by_model == "anthropic/fake-model"
        assert [s["kind"] for s in stored.sources or []] == ["community", "reddit"]
        usage = (await db.execute(select(AiUsageRecord))).scalars().all()
        assert [(u.input_tokens, u.output_tokens) for u in usage] == [(100, 20)]
    assert ("release.assessed", None) in notified
    assert [n for n in notified if n[0] == "update.blocked"] and published

    # A re-assessment that confirms STOP doesn't notify "blocked" again.
    notified.clear()
    await release_jobs._assess_release(str(release.id))
    assert [n for n in notified if n[0] == "update.blocked"] == []


async def test_assess_records_an_unusable_answer(
    jobs_db: Any, no_community: list[Any], published: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure_model(jobs_db)
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))

    async def _evidence(app_settings: Any, release: Release) -> Evidence:
        return Evidence()

    monkeypatch.setattr(release_jobs, "gather_evidence", _evidence)
    monkeypatch.setattr(release_jobs, "build_client", lambda provider: _FakeClient("I can't."))
    async with jobs_db() as db:
        release = (await db.execute(select(Release))).scalars().first()
        assert release is not None
    result = await release_jobs._assess_release(str(release.id))
    assert result["ok"] is False
    async with jobs_db() as db:
        stored = await db.get(Release, release.id)
        assert stored is not None and stored.risk_level is None
        assert "no JSON" in (stored.assessment_error or "")


async def test_reassess_picks_young_stale_pending_releases(
    jobs_db: Any, no_community: list[Any], published: list[Any], celery_calls: Any
) -> None:
    await _configure_model(jobs_db)
    fabric_id = await _fabric_with_inventory(jobs_db)
    await release_jobs._discover_releases(str(fabric_id))
    now = datetime.now(UTC)
    async with jobs_db() as db:
        for release in (await db.execute(select(Release))).scalars():
            release.risk_level = 2
            release.assessed_at = now - timedelta(days=2)
            release.published_at = now - timedelta(days=5)
            if release.product == "protect":
                release.manual_risk_level = 1
        await db.commit()
    celery_calls.clear()
    # Seven pending or installed, both Protect ones rated by hand.
    assert await release_jobs._reassess_releases(now) == 5


# --- web --------------------------------------------------------------------------


async def _web_release(factory: Any, fabric_id: uuid.UUID, **kwargs: Any) -> uuid.UUID:
    async with factory() as db:
        console = _console()
        console.fabric_id = fabric_id
        db.add(console)
        release = Release(kind="app", product="protect", version="7.3.74", **kwargs)
        db.add(release)
        await db.commit()
        return release.id


async def test_fabric_view_shows_target_versions_with_verdicts_and_note_links(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    await _web_release(db_session_factory, fabric_id, risk_level=4)
    page = await client.get(f"/fabrics/{fabric_id}")
    assert page.status_code == 200
    assert "7.3.74" in page.text and "5.5.51" in page.text
    assert "STOP" in page.text
    assert "/releases/notes?kind=app&amp;product=protect&amp;version=7.3.70" in page.text
    assert "risk 4/5" in page.text


async def test_release_pages_and_permissions(
    client: Any, db_session_factory: Any, accept_any_api_key: Any, login_as: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    release_id = await _web_release(
        db_session_factory, fabric_id, risk_level=4, reason="STP broken, cameras flicker"
    )

    listing = await client.get("/assessments")
    assert listing.status_code == 200
    assert "STP broken, cameras flicker" in listing.text
    # Current → new.
    assert "version=7.3.70" in listing.text and "→" in listing.text
    assert f"/releases/{release_id}" in listing.text
    detail = await client.get(f"/releases/{release_id}")
    assert detail.status_code == 200 and "HQ" in detail.text

    csrf = client.cookies.get("csrftoken")
    response = await client.post(
        f"/releases/{release_id}/risk",
        data={"risk_level": "2", "note": "tested", "csrf_token": csrf},
    )
    assert response.status_code == 303
    response = await client.post(f"/releases/{release_id}/allow", data={"csrf_token": csrf})
    assert response.status_code == 303
    assert (
        await client.post(f"/releases/{release_id}/allow", data={"csrf_token": "x"})
    ).status_code == 403
    async with db_session_factory() as db:
        release = await db.get(Release, release_id)
        assert release is not None
        assert release.manual_risk_level == 2 and release.is_allowed
        actions = {e.action for e in (await db.execute(select(AuditLogEntry))).scalars()}
        assert {"release.risk.set", "release.allow"} <= actions

    await login_as(client, permissions={Permission.FABRIC_VIEW}, username="viewer")
    assert (await client.get(f"/releases/{release_id}")).status_code == 200
    csrf = client.cookies.get("csrftoken")
    assert (
        await client.post(
            f"/releases/{release_id}/risk", data={"risk_level": "1", "csrf_token": csrf}
        )
    ).status_code == 403
    assert (
        await client.post(f"/releases/{release_id}/revoke", data={"csrf_token": csrf})
    ).status_code == 403

    await login_as(client, permissions=set(), username="nobody")
    assert (await client.get("/releases")).status_code == 403
    assert (await client.get("/assessments")).status_code == 403


async def test_release_detail_lists_only_visible_fabrics(
    client: Any, db_session_factory: Any, accept_any_api_key: Any, login_as: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    visible = await create_fabric_via_web(client, "Visible")
    hidden = await create_fabric_via_web(client, "Hidden")
    release_id = await _web_release(db_session_factory, visible)
    async with db_session_factory() as db:
        console = _console()
        console.name = "SecretConsole"
        console.fabric_id = hidden
        db.add(console)
        await db.commit()
    await login_as(client, permissions={Permission.FABRIC_VIEW}, fabric_ids={visible})
    page = await client.get(f"/releases/{release_id}")
    assert page.status_code == 200
    assert "Visible" in page.text and "SecretConsole" not in page.text


async def test_release_notes_redirects_only_to_community(
    client: Any, db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _find(self: Any, kind: str, product: str, version: str) -> ReleasePost | None:
        if version == "11.0.104":
            return ReleasePost("r2", "UniFi Network Application", "11.0.104", "GA",
                               "https://community.ui.com/releases/x/r2", None)  # fmt: skip
        return None

    monkeypatch.setattr(CommunityClient, "find_release", _find)
    found = await client.get("/releases/notes?kind=app&product=network&version=11.0.104")
    assert found.status_code == 302
    assert found.headers["location"] == "https://community.ui.com/releases/x/r2"
    fallback = await client.get("/releases/notes?kind=app&product=network&version=9.9.9")
    assert fallback.headers["location"].startswith("https://community.ui.com/releases?q=")

    async with db_session_factory() as db:
        db.add(Release(kind="app", product="talk", version="1.2.3",
                       community_url="https://evil.example/phish"))  # fmt: skip
        await db.commit()
    stored_bad = await client.get("/releases/notes?kind=app&product=talk&version=1.2.3")
    assert stored_bad.headers["location"].startswith("https://community.ui.com/")
    for bad in ("kind=x&product=a&version=1.0.0", "kind=app&product=&version=1.0.0",
                "kind=app&product=a&version=nope"):  # fmt: skip
        assert (await client.get(f"/releases/notes?{bad}")).status_code == 404


async def test_release_settings_are_validated_and_secrets_encrypted(
    client: Any, db_session_factory: Any
) -> None:
    await client.get("/settings?tab=releases")
    csrf = client.cookies.get("csrftoken")
    bad = await client.post(
        "/settings/releases", data={"max_acceptable_risk_level": "9", "csrf_token": csrf}
    )
    assert bad.status_code == 200 and "between 1 and 5" in bad.text
    ok = await client.post(
        "/settings/releases",
        data={
            "max_acceptable_risk_level": "3",
            "reddit_client_id": "rid",
            "reddit_client_secret": "rsecret",
            "web_search_kind": "searxng",
            "web_search_url": "https://search.example",
            "csrf_token": csrf,
        },
    )
    assert ok.status_code == 303
    async with db_session_factory() as db:
        app_settings = await get_or_create_app_settings(db)
        assert app_settings.max_acceptable_risk_level == 3
        assert app_settings.reddit_client_secret_encrypted is not None
        assert decrypt_secret(app_settings.reddit_client_secret_encrypted) == "rsecret"
        assert app_settings.web_search_kind == "searxng"
    page = await client.get("/settings?tab=releases")
    assert "rsecret" not in page.text
    brave = await client.post(
        "/settings/releases",
        data={"max_acceptable_risk_level": "3", "web_search_kind": "brave", "csrf_token": csrf},
    )
    assert "Brave Search needs an API key" in brave.text


async def test_scheduled_reassess_action_enqueues_per_release(
    db_session_factory: Any, no_community: list[Any], celery_calls: Any
) -> None:
    from app.scheduling.actions import get_action
    from app.scheduling.builtin_actions import register_builtin_actions
    from app.scheduling.targets import ResolvedTarget

    register_builtin_actions()
    action = get_action("assess_releases")
    assert action is not None and action.extra_permission is Permission.RELEASE_MANAGE
    await _configure_model(db_session_factory)
    fabric_id = await _fabric_with_inventory(db_session_factory)
    async with db_session_factory() as db:
        fabric = await db.get(Fabric, fabric_id)
        assert fabric is not None
        result = await action.run(db, ResolvedTarget(fabrics=[fabric]), {})
        devices = (await db.execute(select(Device))).scalars().all()
    assert result.attempted == 2
    assert celery_calls.names.count("app.tasks.release_jobs.assess_release") == 2
    assert len(devices) == 3


# --- 0.2.2: the catalogue, Assessments, the assistant's release tools ----------


async def test_catalog_refresh_keeps_the_newest_three_and_queues_them(
    jobs_db: Any, monkeypatch: pytest.MonkeyPatch, celery_calls: Any
) -> None:
    from app.releases import service

    async def _search(self: Any, term: str, limit: int = 25) -> list[Any]:
        if term != "UniFi Protect Application":
            return []
        return [
            {"id": f"p{n}", "slug": "s", "title": "UniFi Protect Application",
             "version": f"7.3.{n}", "stage": "GA", "createdAt": "2026-11-01T00:00:00Z"}
            for n in (70, 74, 61, 50)
        ] + [{"id": "x", "title": "UniFi Protect iOS", "version": "9.9.9"}]  # fmt: skip

    monkeypatch.setattr(CommunityClient, "search", _search)
    await _configure_model(jobs_db)
    result = await release_jobs._refresh_release_catalog()
    assert result == {"ok": True, "catalog": 3, "queued": 3}
    async with jobs_db() as db:
        groups = {g.products[0]: g for g in await service.catalog(db)}
        rows = groups["protect"].releases
        assert [r.version for r in rows] == ["7.3.74", "7.3.70", "7.3.61"]
        assert rows[0].community_url == "https://community.ui.com/releases/s/p74"
    assert celery_calls.names.count("app.tasks.release_jobs.assess_release") == 3
    # Already queued: a second refresh queues nothing.
    assert (await release_jobs._refresh_release_catalog())["queued"] == 0


async def test_catalog_and_assessments_pages(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    await _web_release(db_session_factory, fabric_id, risk_level=2, reason="no notable reports")
    async with db_session_factory() as db:
        db.add(
            Release(
                kind="app", product="protect", version="7.3.70", title="UniFi Protect Application",
                community_release_id="c1", community_url="https://community.ui.com/releases/x/c1",
                risk_level=4, reason="cameras flicker",
                sources=[{"kind": "reddit", "title": "t", "url": "https://www.reddit.com/r/x"}],
            )
        )  # fmt: skip
        await db.commit()
    page = await client.get("/releases")
    assert page.status_code == 200
    assert "UniFi Protect Application" in page.text and "cameras flicker" in page.text
    assert 'href="https://www.reddit.com/r/x"' in page.text
    # Every row has its own way to the full assessment, not only "+N" sources.
    assert page.text.count(">More info</a>") == 1
    # The console in this Fabric runs 7.3.70: counted under "your consoles".
    assert ">1</td>" in page.text
    assessments = await client.get("/assessments")
    assert assessments.status_code == 200 and "no notable reports" in assessments.text
    assert assessments.text.count(">More info</a>") == 1
    nav = await client.get("/dashboard")
    assert 'href="/assessments"' in nav.text and 'href="/releases"' in nav.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Protect", ("app", "protect")),
        ("UniFi Network Application", ("app", "network")),
        ("unifi network", ("app", "network")),
        ("InnerSpace", ("app", "innerspace")),
        ("G5 Flex", (None, "G5 Flex")),
    ],
)
def test_resolve_product(text: str, expected: tuple[str | None, str]) -> None:
    from app.ai.release_tools import resolve_product

    assert resolve_product(text) == expected


async def test_assistant_release_tools(
    client: Any, db_session_factory: Any, accept_any_api_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai import release_tools
    from app.ai.tools import TOOL_SPECS, available_tools, execute_read_only_tool
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    await _web_release(
        db_session_factory, fabric_id, risk_level=4, reason="STP broken", summary="Bad.",
        issues=[{"title": "STP", "detail": "loops"}],
        sources=[{"kind": "community", "title": "notes", "url": "https://community.ui.com/r"}],
    )  # fmt: skip
    for name in ("list_release_ratings", "get_release_rating", "read_release_reports"):
        assert name in TOOL_SPECS and not TOOL_SPECS[name].mutating

    async with db_session_factory() as db:
        from app.db.models.user import User

        user = (await db.execute(select(User))).scalars().first()
        assert user is not None
        assert "read_release_reports" in [t.name for t in available_tools(user)]

        listing = await execute_read_only_tool(
            db,
            user,
            ToolCall(id="1", name="list_release_ratings", arguments={"product": "Protect"}),
        )
        assert "7.3.74" in listing and "risk 4/5" in listing and "STP broken" in listing
        assert "pending on 1" in listing and "1 STOP" in listing

        detail = await execute_read_only_tool(
            db, user,
            ToolCall(id="2", name="get_release_rating",
                     arguments={"product": "protect", "version": "7.3.74"}),
        )  # fmt: skip
        assert "Why: STP broken" in detail and "issue: STP loops" in detail
        assert "https://community.ui.com/r" in detail and "Office: HQ" in detail

        missing = await execute_read_only_tool(
            db, user,
            ToolCall(id="3", name="get_release_rating",
                     arguments={"product": "network", "version": "1.0.0"}),
        )  # fmt: skip
        assert "read_release_reports" in missing

        async def _evidence(app_settings: Any, release: Release) -> Evidence:
            assert (release.kind, release.product, release.version) == (
                "app",
                "network",
                "11.0.104",
            )
            evidence = Evidence()
            evidence.add(
                EvidenceItem(
                    kind="reddit",
                    title="VLANs dropped",
                    url="https://www.reddit.com/r/y",
                    text="after 11.0.104 my VLANs dropped",
                    score=50,
                )
            )
            return evidence

        monkeypatch.setattr(release_tools, "gather_evidence", _evidence)
        reports = await execute_read_only_tool(
            db, user,
            ToolCall(id="4", name="read_release_reports",
                     arguments={"product": "UniFi Network", "version": "11.0.104"}),
        )  # fmt: skip
        assert "VLANs dropped" in reports and "not a ucc rating" in reports
        assert "<evidence>" in reports
        bad = await execute_read_only_tool(
            db, user, ToolCall(id="5", name="read_release_reports", arguments={"product": "x"})
        )
        assert "required" in bad


# --- 0.2.3: catalogue refresh, model names, report weighting -----------------------


async def test_catalog_refresh_button(
    client: Any, db_session_factory: Any, login_as: Any, celery_calls: Any
) -> None:
    page = await client.get("/releases")
    assert 'action="/releases/refresh"' in page.text
    csrf = client.cookies.get("csrftoken")
    response = await client.post("/releases/refresh", data={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["location"] == "/releases?refreshing=1"
    assert "app.tasks.release_jobs.refresh_release_catalog" in celery_calls.names
    assert (await client.post("/releases/refresh", data={"csrf_token": "x"})).status_code == 403
    async with db_session_factory() as db:
        actions = {e.action for e in (await db.execute(select(AuditLogEntry))).scalars()}
        assert "release.catalog.refresh" in actions

    await login_as(client, permissions={Permission.FABRIC_VIEW}, username="viewer")
    assert 'action="/releases/refresh"' not in (await client.get("/releases")).text
    csrf = client.cookies.get("csrftoken")
    assert (await client.post("/releases/refresh", data={"csrf_token": csrf})).status_code == 403


def test_catalog_interval_comes_from_settings_and_beat_start_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import celery_app as module

    entry = module.celery_app.conf.beat_schedule["refresh-release-catalog"]
    assert entry["schedule"] == timedelta(hours=24)
    sent: list[str] = []
    monkeypatch.setattr(module.celery_app, "send_task", lambda name, **_: sent.append(name))
    module._on_beat_start()
    assert sent == ["app.tasks.release_jobs.refresh_release_catalog"]


async def test_catalog_interval_setting_is_validated(client: Any, db_session_factory: Any) -> None:
    page = await client.get("/settings?tab=checks")
    assert 'name="release_catalog_interval_hours"' in page.text and 'value="24"' in page.text
    csrf = client.cookies.get("csrftoken")
    response = await client.post(
        "/settings/background-checks",
        data={"inventory_sync_interval_seconds": "900", "release_catalog_interval_hours": "12",
              "csrf_token": csrf},
    )  # fmt: skip
    assert response.status_code == 303
    bad = await client.post(
        "/settings/background-checks",
        data={"inventory_sync_interval_seconds": "900", "release_catalog_interval_hours": "0",
              "csrf_token": csrf},
    )  # fmt: skip
    assert bad.status_code == 200 and "between 1 and 168" in bad.text
    # A form without the field (pre-0.2.3) keeps the stored value.
    await client.post(
        "/settings/background-checks",
        data={"inventory_sync_interval_seconds": "600", "csrf_token": csrf},
    )
    async with db_session_factory() as db:
        app_settings = await get_or_create_app_settings(db)
        assert app_settings.release_catalog_interval_hours == 12
        assert app_settings.inventory_sync_interval_seconds == 600


async def test_model_names_show_instead_of_codes(
    client: Any, db_session_factory: Any, accept_any_api_key: Any
) -> None:
    from app.ai.release_tools import _model_code
    from app.releases.service import model_names
    from tests.conftest import create_fabric_via_web

    fabric_id = await create_fabric_via_web(client, "Office")
    async with db_session_factory() as db:
        console = _console()
        console.fabric_id = fabric_id
        console.model, console.model_name = "UDRULT", "UCG-Ultra"
        console.devices[0].model_name = "G5-Flex"
        db.add(console)
        release = Release(kind="os", product="UDRULT", version="4.4.6", risk_level=1)
        db.add(release)
        await db.commit()
        assert (await model_names(db))["UDRULT"] == "UCG-Ultra"
        assert await _model_code(db, "ucg ultra") == "UDRULT"
        assert await _model_code(db, "Something else") == "Something else"
    page = await client.get(f"/fabrics/{fabric_id}")
    assert "UCG-Ultra" in page.text and "G5-Flex" in page.text
    detail = await client.get(f"/releases/{release.id}")
    assert 'title="UDRULT">UCG-Ultra<' in detail.text


def test_prompt_weighs_independent_reports_and_issues_carry_counts() -> None:
    assert "independent reports" in assess_module.SYSTEM_PROMPT
    assert "anecdote" in assess_module.SYSTEM_PROMPT
    result = parse_answer(
        json.dumps({"risk_level": 3, "reason": "r", "summary": "s",
                    "issues": [{"title": "STP", "detail": "loops", "reports": 5},
                               {"title": "LED", "detail": "one user"}]})
    )  # fmt: skip
    assert [i.reports for i in result.issues] == [5, None]
    with pytest.raises(AssessmentError):
        parse_answer(
            json.dumps({"risk_level": 3, "summary": "s",
                        "issues": [{"title": "STP", "reports": True}]})
        )  # fmt: skip
