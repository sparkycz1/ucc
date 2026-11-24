"""AI assistant: permission gating, the confirm-before-execute gate, token
limits, and conversation ownership.

No provider is ever contacted here — `app.tasks.ai_jobs.build_client` is
monkeypatched to a fake client whose canned responses drive the turn loop,
which also lets the tests assert on exactly *which tools* were offered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.ai.base import BaseAiClient, ChatTurnResult, ModelInfo, ToolCall
from app.ai.usage import (
    DAY_WINDOW,
    MONTH_WINDOW,
    WEEK_WINDOW,
    check_within_limits,
    get_usage_totals,
)
from app.core.security import decrypt_secret, encrypt_secret
from app.db import session as db_session
from app.db.models.ai_conversation import AiConversation
from app.db.models.ai_message import AiMessage, AiMessageRole
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig, AiProviderKind
from app.db.models.ai_usage import AiUsageRecord
from app.db.models.app_settings import SINGLETON_ID, AppSettings
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.role import Permission
from app.db.models.user import User
from app.tasks import ai_jobs
from app.web.routes import settings as settings_routes
from tests.conftest import ADMIN_USERNAME

ALL_AI_PERMISSIONS = {Permission.AI_ACCESS, Permission.FABRIC_VIEW}


class FakeClient(BaseAiClient):
    """Anthropic-shaped fake — enough to exercise the loop and record what
    the turn task offered the model."""

    kind_value = "anthropic"

    def __init__(self, results: list[ChatTurnResult]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def list_models(self) -> list[Any]:
        return []

    async def send(
        self, messages: list[Any], tools: list[Any], model: str, system_prompt: str
    ) -> ChatTurnResult:
        self.calls.append({"messages": list(messages), "tools": list(tools), "model": model})
        return self.results.pop(0)

    def build_user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def build_tool_result_messages(self, result: ChatTurnResult, outputs: list[Any]) -> list[Any]:
        return [
            result.raw_assistant_message,
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": output}
                    for call, output in outputs
                ],
            },
        ]


def text_turn(text: str) -> ChatTurnResult:
    return ChatTurnResult(
        text=text,
        input_tokens=10,
        output_tokens=5,
        raw_assistant_message={"role": "assistant", "content": text},
    )


def tool_turn(name: str, arguments: dict[str, Any]) -> ChatTurnResult:
    call = ToolCall(id="tu_1", name=name, arguments=arguments)
    return ChatTurnResult(
        text=None,
        tool_calls=[call],
        input_tokens=10,
        output_tokens=5,
        raw_assistant_message={"role": "assistant", "content": [{"type": "tool_use"}]},
    )


@pytest.fixture
def use_test_db(db_session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`app.tasks.ai_jobs` opens its own session through
    `app.db.session.AsyncSessionLocal` (see that module's docstring on why it
    goes through the module) — point it at the test's SQLite factory."""
    monkeypatch.setattr(db_session, "AsyncSessionLocal", db_session_factory)
    return db_session_factory


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch, results: list[ChatTurnResult]
) -> FakeClient:
    fake = FakeClient(results)
    monkeypatch.setattr(ai_jobs, "build_client", lambda config: fake)
    return fake


async def setup_provider(
    db_session_factory: Any, *, model_id: str = "fake-model"
) -> tuple[uuid.UUID, str]:
    async with db_session_factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.ANTHROPIC,
            enabled=True,
            # A validly-encrypted (if fake) key — needed because
            # `app.web.routes.ai.post_message` now calls the *real*
            # `build_client` itself (only `.build_user_message()`, never
            # `.send()`, which stays test-doubled via `install_fake_client`
            # patching `ai_jobs.build_client` for the actual turn). A
            # decryptable value here, not the network, is what that needs.
            api_key_encrypted=encrypt_secret("fake-key"),
        )
        db.add(provider)
        await db.flush()
        db.add(AiModel(provider_id=provider.id, model_id=model_id, enabled=True))
        await db.commit()
        return provider.id, model_id


async def get_user(db_session_factory: Any, username: str = ADMIN_USERNAME) -> User:
    async with db_session_factory() as db:
        result = await db.execute(select(User).where(User.username == username))
        user: User = result.scalar_one()
        return user


async def create_conversation(
    db_session_factory: Any, user_id: uuid.UUID, provider_id: uuid.UUID, model_id: str
) -> uuid.UUID:
    async with db_session_factory() as db:
        conversation = AiConversation(
            user_id=user_id, title="t", provider_id=provider_id, model_id=model_id
        )
        db.add(conversation)
        await db.commit()
        return conversation.id


async def create_device(
    db_session_factory: Any, name: str = "ap-lobby", *, fabric_name: str = "Office"
) -> uuid.UUID:
    """A Fabric with one console and one device that has an update pending."""
    async with db_session_factory() as db:
        fabric = Fabric(name=fabric_name, api_key_encrypted=encrypt_secret("test-key"))
        console = Console(fabric=fabric, host_id=f"host-{fabric_name}", name="udm")
        device = Device(
            console=console,
            mac="aa:bb:cc:00:00:01",
            name=name,
            model="U6-Pro",
            version="6.6.77",
            update_available="6.7.10",
            is_online=True,
        )
        db.add_all([fabric, console, device])
        await db.commit()
        return fabric.id


async def pending_actions_of(
    db_session_factory: Any, conversation_id: uuid.UUID
) -> tuple[uuid.UUID | None, list[dict[str, Any]]]:
    async with db_session_factory() as db:
        result = await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.created_at, AiMessage.id)
        )
        for message in reversed(list(result.scalars().all())):
            if message.pending_actions:
                return message.id, message.pending_actions
    return None, []


# --- Sending a message: async turn, not a blocking wait ---------------------
#
# `post_message` used to enqueue the turn and block the whole HTTP request on
# `AsyncResult.get(timeout=90)` — a real bug (see app/tasks/ai_jobs.py's
# `_AI_TURN_TIME_LIMIT_SECONDS` comment): a turn can make several sequential
# provider calls, easily exceeding both that wait and the Celery task's own
# time limit, which could kill the task before either timeout ever fired,
# silently. It now persists the human's message immediately, enqueues the
# turn, and redirects without waiting — `partials/ai_messages_panel.html`
# polls for the reply. These tests exercise that route directly (never
# `_run_turn`), so `celery_calls`'s autouse `.delay()` stub means the turn
# itself never actually runs — exactly the "still waiting" state being
# tested.


async def test_sending_a_message_persists_it_immediately_and_redirects(
    client, db_session_factory, celery_calls
):
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    await client.get(f"/ai/conversations/{conversation_id}")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        f"/ai/conversations/{conversation_id}/messages",
        data={"csrf_token": csrf_token, "message": "hello there"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/ai/conversations/{conversation_id}"

    async with db_session_factory() as db:
        result = await db.execute(
            select(AiMessage).where(AiMessage.conversation_id == conversation_id)
        )
        messages = list(result.scalars().all())
    assert len(messages) == 1
    assert messages[0].content == "hello there"
    assert messages[0].role.value == "user"
    # Provider-native shape built eagerly too (not left for the task) — see
    # post_message's docstring.
    assert messages[0].provider_native == [{"role": "user", "content": "hello there"}]

    # Enqueued with just the conversation id — the task reads the message
    # back from history rather than receiving it as an argument, now that
    # the route itself is what persisted it.
    assert celery_calls.names == ["app.tasks.ai_jobs.run_ai_turn"]
    assert celery_calls[0][1] == (str(conversation_id),)


async def test_conversation_page_shows_disabled_form_while_awaiting_reply(
    client, db_session_factory, celery_calls
):
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    await client.get(f"/ai/conversations/{conversation_id}")
    csrf_token = client.cookies.get("csrftoken")
    await client.post(
        f"/ai/conversations/{conversation_id}/messages",
        data={"csrf_token": csrf_token, "message": "hello there"},
    )

    page = await client.get(f"/ai/conversations/{conversation_id}")
    assert page.status_code == 200
    assert "hello there" in page.text
    assert "disabled" in page.text
    assert f'hx-get="/ai/conversations/{conversation_id}/messages-panel"' in page.text
    assert 'hx-trigger="every 2s"' in page.text


async def test_messages_panel_stops_polling_once_the_reply_arrives(
    client, db_session_factory, celery_calls
):
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    await client.get(f"/ai/conversations/{conversation_id}")
    csrf_token = client.cookies.get("csrftoken")
    await client.post(
        f"/ai/conversations/{conversation_id}/messages",
        data={"csrf_token": csrf_token, "message": "hello there"},
    )

    # Simulate the (never-actually-run, per celery_calls) task completing.
    async with db_session_factory() as db:
        db.add(
            AiMessage(
                conversation_id=conversation_id,
                role=AiMessageRole.ASSISTANT,
                content="Hi! How can I help?",
                provider_native=[{"role": "assistant", "content": "Hi! How can I help?"}],
            )
        )
        await db.commit()

    panel = await client.get(f"/ai/conversations/{conversation_id}/messages-panel")
    assert panel.status_code == 200
    assert "Hi! How can I help?" in panel.text
    assert "hx-trigger" not in panel.text
    assert "disabled" not in panel.text


# --- Permission gating -------------------------------------------------------


async def test_every_ai_route_is_refused_without_ai_access(client, login_as):
    await login_as(client, permissions={Permission.FABRIC_VIEW})
    some_id = uuid.uuid4()

    assert (await client.get("/ai")).status_code == 403
    assert (await client.post("/ai/conversations", data={})).status_code == 403
    assert (await client.get(f"/ai/conversations/{some_id}")).status_code == 403
    assert (await client.post(f"/ai/conversations/{some_id}/messages", data={})).status_code == 403
    assert (
        await client.post(f"/ai/conversations/{some_id}/messages/{some_id}/confirm", data={})
    ).status_code == 403
    assert (
        await client.post(f"/ai/conversations/{some_id}/messages/{some_id}/discard", data={})
    ).status_code == 403
    assert (await client.post(f"/ai/conversations/{some_id}/delete", data={})).status_code == 403


async def test_ai_page_is_reachable_with_only_ai_access(client, login_as):
    await login_as(client, permissions={Permission.AI_ACCESS})
    response = await client.get("/ai")
    assert response.status_code == 200
    assert "No AI model is enabled yet" in response.text


async def test_tools_offered_are_filtered_by_the_users_permissions(
    client, login_as, db_session_factory, use_test_db, monkeypatch
):
    """A user without `fabric.view` must never even be *offered* the
    inventory lookups — permission check #1."""
    user = await login_as(client, permissions={Permission.AI_ACCESS}, username="no-fabric-view")
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    fake = install_fake_client(monkeypatch, [text_turn("Sure.")])
    result = await ai_jobs._run_turn(str(conversation_id), "hi", allow_tools=True)

    assert result["ok"] is True
    assert {tool.name for tool in fake.calls[0]["tools"]} == set()


async def test_a_tool_call_the_user_may_not_use_is_refused(
    client, login_as, db_session_factory, use_test_db, monkeypatch, celery_calls
):
    """Permission check #2: even if the model calls a tool that was never
    offered, it gets a refusal, never the data."""
    user = await login_as(client, permissions={Permission.AI_ACCESS}, username="no-fabric-view-2")
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)
    await create_device(db_session_factory, "ap-secret")

    fake = install_fake_client(
        monkeypatch, [tool_turn("list_devices", {}), text_turn("I can't see that.")]
    )
    await ai_jobs._run_turn(str(conversation_id), "list devices", allow_tools=True)

    second_request = str(fake.calls[1]["messages"])
    assert "Refused" in second_request
    assert "ap-secret" not in second_request
    assert celery_calls.names == []


async def test_an_unknown_mutating_tool_call_is_recorded_as_denied(
    client, db_session_factory, use_test_db, monkeypatch, celery_calls
):
    """No mutating tool exists; a hallucinated one is visible as denied."""
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    install_fake_client(
        monkeypatch,
        [tool_turn("reboot_everything", {}), text_turn("I could not do that.")],
    )
    await ai_jobs._run_turn(str(conversation_id), "reboot all", allow_tools=True)

    _message_id, actions = await pending_actions_of(db_session_factory, conversation_id)
    assert len(actions) == 1
    assert actions[0]["status"] == "denied"
    assert celery_calls.names == []


# --- Token limits ------------------------------------------------------------


async def test_a_turn_over_the_daily_limit_is_rejected_before_any_provider_call(
    client, db_session_factory, use_test_db, monkeypatch
):
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    async with db_session_factory() as db:
        db.add(AppSettings(id=SINGLETON_ID, ai_daily_token_limit=100))
        db.add(
            AiUsageRecord(
                user_id=user.id,
                provider_kind=AiProviderKind.ANTHROPIC,
                model_id=model_id,
                input_tokens=90,
                output_tokens=30,
            )
        )
        await db.commit()

    fake = install_fake_client(monkeypatch, [text_turn("should never be sent")])
    result = await ai_jobs._run_turn(str(conversation_id), "hello", allow_tools=True)

    assert result["ok"] is False
    assert "Daily AI token limit (100) reached" in result["error"]
    assert fake.calls == []


# --- Read-only lookups ------------------------------------------------------


async def test_read_only_lookups_execute_immediately_and_feed_back(
    client, db_session_factory, use_test_db, monkeypatch, celery_calls
):
    user = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)
    await create_device(db_session_factory, "ap-lobby")

    fake = install_fake_client(
        monkeypatch,
        [tool_turn("list_devices", {"updates_only": True}), text_turn("ap-lobby has 6.7.10.")],
    )
    result = await ai_jobs._run_turn(str(conversation_id), "what needs updates?", allow_tools=True)

    assert result["ok"] is True
    assert result["pending_action_count"] == 0
    assert len(fake.calls) == 2
    second_request = str(fake.calls[1]["messages"])
    assert "ap-lobby" in second_request
    assert "6.7.10" in second_request
    assert celery_calls.names == []


async def test_device_lookup_is_scoped_to_the_users_fabrics(
    client, login_as, db_session_factory, use_test_db, monkeypatch
):
    """A restricted account's assistant sees only its own Fabrics."""
    office_id = await create_device(db_session_factory, "ap-office", fabric_name="Office")
    await create_device(db_session_factory, "ap-other", fabric_name="Other")
    user = await login_as(
        client,
        permissions={Permission.AI_ACCESS, Permission.FABRIC_VIEW},
        username="office-only",
        fabric_ids={office_id},
    )
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, user.id, provider_id, model_id)

    fake = install_fake_client(monkeypatch, [tool_turn("list_devices", {}), text_turn("Done.")])
    await ai_jobs._run_turn(str(conversation_id), "list devices", allow_tools=True)

    second_request = str(fake.calls[1]["messages"])
    assert "ap-office" in second_request
    assert "ap-other" not in second_request


# --- Ownership ---------------------------------------------------------------


async def test_a_conversation_is_invisible_to_another_user(
    client, login_as, db_session_factory, use_test_db, monkeypatch
):
    owner = await get_user(db_session_factory)
    provider_id, model_id = await setup_provider(db_session_factory)
    conversation_id = await create_conversation(db_session_factory, owner.id, provider_id, model_id)

    await login_as(client, permissions=ALL_AI_PERMISSIONS, username="someone-else")
    await client.get("/ai")
    csrf_token = client.cookies.get("csrftoken")
    fake_message_id = uuid.uuid4()

    assert (await client.get(f"/ai/conversations/{conversation_id}")).status_code == 404
    assert (
        await client.post(
            f"/ai/conversations/{conversation_id}/messages",
            data={"csrf_token": csrf_token, "message": "hi"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/ai/conversations/{conversation_id}/messages/{fake_message_id}/confirm",
            data={"csrf_token": csrf_token, "action_index": "0"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/ai/conversations/{conversation_id}/delete", data={"csrf_token": csrf_token}
        )
    ).status_code == 404


async def test_settings_page_never_echoes_a_stored_api_key(client, db_session_factory):
    """The key is write-only from the browser's side: the page may say one
    exists, never what it is."""
    async with db_session_factory() as db:
        db.add(
            AiProviderConfig(
                kind=AiProviderKind.ANTHROPIC,
                enabled=True,
                api_key_encrypted=encrypt_secret("sk-super-secret-value"),
            )
        )
        await db.commit()

    response = await client.get("/settings?tab=ai")
    assert response.status_code == 200
    assert "sk-super-secret-value" not in response.text
    assert "unchanged" in response.text


async def test_saving_a_provider_with_a_blank_key_keeps_the_existing_one(
    client, db_session_factory
):
    async with db_session_factory() as db:
        db.add(
            AiProviderConfig(
                kind=AiProviderKind.OPENAI,
                enabled=False,
                api_key_encrypted=encrypt_secret("sk-original"),
            )
        )
        await db.commit()

    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/settings/ai/provider",
        data={"csrf_token": csrf_token, "kind": "openai", "enabled": "1", "api_key": ""},
    )
    assert response.status_code == 303

    async with db_session_factory() as db:
        result = await db.execute(
            select(AiProviderConfig).where(AiProviderConfig.kind == AiProviderKind.OPENAI)
        )
        config = result.scalar_one()
    assert config.enabled is True
    assert decrypt_secret(config.api_key_encrypted) == "sk-original"


async def test_enabling_an_openai_compatible_provider_requires_a_base_url(
    client, db_session_factory
):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/settings/ai/provider",
        data={
            "csrf_token": csrf_token,
            "kind": "openai_compatible",
            "enabled": "1",
            "api_key": "sk-x",
            "base_url": "",
        },
    )
    assert response.status_code == 200
    assert "needs a base URL" in response.text


async def test_refetching_models_preserves_the_enabled_flag_and_drops_stale_ids(
    client, db_session_factory, monkeypatch
):
    async with db_session_factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.ANTHROPIC, enabled=True, api_key_encrypted=encrypt_secret("k")
        )
        db.add(provider)
        await db.flush()
        db.add_all(
            [
                AiModel(provider_id=provider.id, model_id="keep-me", enabled=True),
                AiModel(provider_id=provider.id, model_id="gone-upstream", enabled=True),
            ]
        )
        await db.commit()

    class StubClient:
        async def list_models(self):
            return [ModelInfo(id="keep-me"), ModelInfo(id="brand-new")]

    monkeypatch.setattr(settings_routes, "build_client", lambda config: StubClient())

    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/settings/ai/fetch-models", data={"csrf_token": csrf_token, "kind": "anthropic"}
    )
    assert response.status_code == 303

    async with db_session_factory() as db:
        rows = (await db.execute(select(AiModel))).scalars().all()
    by_id = {row.model_id: row for row in rows}
    assert set(by_id) == {"keep-me", "brand-new"}
    assert by_id["keep-me"].enabled is True
    # A newly discovered model is never auto-approved for chat.
    assert by_id["brand-new"].enabled is False


async def test_usage_windows_are_rolling_not_calendar_aligned(db_session_factory):
    now = datetime.now(UTC)
    async with db_session_factory() as db:
        db.add_all(
            [
                # 2 hours ago — inside every window.
                AiUsageRecord(
                    provider_kind=AiProviderKind.OPENAI,
                    model_id="m",
                    input_tokens=10,
                    output_tokens=0,
                    created_at=now - timedelta(hours=2),
                ),
                # 3 days ago — outside 24h, inside 7d/30d.
                AiUsageRecord(
                    provider_kind=AiProviderKind.OPENAI,
                    model_id="m",
                    input_tokens=100,
                    output_tokens=0,
                    created_at=now - timedelta(days=3),
                ),
                # 45 days ago — outside all of them.
                AiUsageRecord(
                    provider_kind=AiProviderKind.OPENAI,
                    model_id="m",
                    input_tokens=1000,
                    output_tokens=0,
                    created_at=now - timedelta(days=45),
                ),
            ]
        )
        await db.commit()

        assert await get_usage_totals(db, now - DAY_WINDOW) == 10
        assert await get_usage_totals(db, now - WEEK_WINDOW) == 110
        assert await get_usage_totals(db, now - MONTH_WINDOW) == 110

        settings_row = AppSettings(id=SINGLETON_ID, ai_weekly_token_limit=110)
        db.add(settings_row)
        await db.commit()
        message = await check_within_limits(db, settings_row)

    assert message is not None
    assert "Weekly AI token limit (110) reached" in message


async def test_no_configured_limit_means_unlimited(db_session_factory):
    async with db_session_factory() as db:
        settings_row = AppSettings(id=SINGLETON_ID)
        db.add(settings_row)
        await db.commit()
        assert await check_within_limits(db, settings_row) is None


async def test_creating_a_conversation_rejects_a_model_that_is_not_enabled(
    client, db_session_factory
):
    async with db_session_factory() as db:
        provider = AiProviderConfig(
            kind=AiProviderKind.ANTHROPIC, enabled=True, api_key_encrypted=b"x"
        )
        db.add(provider)
        await db.flush()
        db.add(AiModel(provider_id=provider.id, model_id="not-approved", enabled=False))
        await db.commit()
        provider_id = provider.id

    await client.get("/ai")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/ai/conversations",
        data={
            "csrf_token": csrf_token,
            "provider_model": f"{provider_id}:not-approved",
            "first_message": "hello",
        },
    )
    assert response.status_code == 400
