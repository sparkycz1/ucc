"""The AI assistant's provider turn, as a Celery job.

Same two-part shape as `app.tasks.jobs` (async implementation + a one-line
synchronous task wrapper) and the same `db_session.AsyncSessionLocal()`
through-the-module rule — see that module's docstring, and
`app.tasks.celery_app`'s, for why both matter.

**Why this is a background task at all**, unlike the LDAP/OIDC HTTP calls
elsewhere in this app: one user message can drive several *sequential*
provider requests (the model asks for a device list, gets it, then asks
for another lookup, then answers), each of which can take tens of seconds
on a slow model, so it runs in Celery — the web route enqueues
and waits on the result with `asyncio.to_thread(async_result.get, ...)`.

**What this task can and cannot do.** It can read the inventory (the
read-only lookups execute here, immediately). It can *record* a proposed
mutating action into `AiMessage.pending_actions`. It can never run one:
nothing in this module touches a UniFi console or device. Executing a
proposal requires a human's confirmed POST — see `app.web.routes.ai`.

Permissions are re-derived from the conversation owner's account **fresh
from the database on every turn**, never from anything cached or passed in
by the caller: a role edited between two messages takes effect on the next
one.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.ai.base import AiProviderError, BaseAiClient, ChatTurnResult, ToolCall
from app.ai.fallback import Candidate, model_chain
from app.ai.providers import build_client
from app.ai.tools import (
    MAX_TOOL_ROUNDTRIPS,
    SYSTEM_PROMPT,
    TOOL_SPECS,
    available_tools,
    build_pending_action,
    execute_read_only_tool,
)
from app.ai.usage import Purpose, check_within_limits, record_usage
from app.audit import log_event
from app.core.app_settings import get_or_create_app_settings
from app.db import session as db_session
from app.db.models.ai_conversation import AiConversation
from app.db.models.ai_message import AiMessage, AiMessageRole
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderKind
from app.db.models.audit_log import AuditOutcome
from app.db.models.user import User
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# What the model is told after a mutating tool call. It is important that
# this reads as "recorded, awaiting a human", not as "done" — the model's
# next sentence to the user is written from it.
_PROPOSAL_FEEDBACK = (
    "Recorded as a proposal and shown to the operator for confirmation. "
    "It has NOT run. Do not propose the same action again; instead tell the "
    "operator what you have proposed and that they need to confirm it."
)

_NO_TEXT_FALLBACK = (
    "(The assistant returned no text for this turn — see the proposed actions below.)"
)


_TRANSCRIPT_INTRO = (
    "(This conversation continues with a different AI model, so its earlier "
    "turns follow as a plain transcript. Answer the last user message.)"
)


async def _rows(db: Any, conversation_id: uuid.UUID) -> list[AiMessage]:
    result = await db.execute(
        select(AiMessage)
        .where(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.created_at, AiMessage.id)
    )
    return list(result.scalars().all())


async def _load_history(
    db: Any, conversation: AiConversation, kind: AiProviderKind, client: BaseAiClient
) -> list[Any]:
    """The history to send `client` (a provider of `kind`).

    Normally every stored provider-native message, oldest first: each
    `AiMessage.provider_native` holds the native messages that turn
    contributed (see `app.db.models.ai_message`), so the history is just
    those lists concatenated — no translation, which is the reason they're
    stored provider-shaped in the first place.

    When some of it is in another provider's format (a turn the fallback
    model answered, or this turn going to the fallback) — or the newest
    user message has none — it can't be replayed natively, and goes as
    one plain-text transcript instead (tool calls and their results are
    left out; the answers that used them are in it)."""
    rows = await _rows(db, conversation.id)
    own_kind = conversation.provider.kind if conversation.provider else None
    last_user = next((r for r in reversed(rows) if r.role == AiMessageRole.USER), None)
    native_ok = (last_user is None or last_user.provider_native is not None) and all(
        (row.provider_kind or own_kind) == kind
        for row in rows
        if row.provider_native is not None
    )
    if native_ok:
        messages: list[Any] = []
        for row in rows:
            native = row.provider_native
            if isinstance(native, list):
                messages.extend(native)
            elif native is not None:
                messages.append(native)
        return messages
    lines = [_TRANSCRIPT_INTRO, ""]
    for row in rows:
        if row.role == AiMessageRole.USER:
            lines.append(f"User: {row.content}")
        elif row.provider_native is not None and row.content:
            # Assistant rows without a native payload are errors ucc wrote.
            lines.append(f"Assistant: {row.content}")
        lines.append("")
    return [client.build_user_message("\n".join(lines).strip())]


async def _persist_assistant_error(
    db: Any, conversation_id: uuid.UUID, message: str
) -> dict[str, Any]:
    """Record a failure as a visible assistant message.

    `provider_native=None` deliberately: an error we generated is not part
    of the provider's own conversation state, and replaying it back to the
    provider next turn would be inventing history it never produced.
    """
    db.add(
        AiMessage(
            conversation_id=conversation_id,
            role=AiMessageRole.ASSISTANT,
            content=message,
            provider_native=None,
        )
    )
    await db.commit()
    return {"ok": False, "error": message}


async def _chain(db: Any, conversation: AiConversation, app_settings: Any) -> list[Candidate]:
    """The conversation's own model first, then the primary and the
    fallback model (Settings → AI) as fallbacks."""
    chain: list[Candidate] = []
    provider = conversation.provider
    if provider is not None and provider.enabled:
        chain.append(
            Candidate(provider, AiModel(provider_id=provider.id, model_id=conversation.model_id))
        )
    for candidate in await model_chain(db, app_settings):
        if not any(
            c.provider.id == candidate.provider.id and c.model.model_id == candidate.model.model_id
            for c in chain
        ):
            chain.append(Candidate(candidate.provider, candidate.model, fallback=True))
    return chain


@dataclass
class _TurnOutcome:
    final_text: str | None = None
    turn_native: list[Any] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


async def _attempt(
    db: Any,
    owner: User,
    conversation: AiConversation,
    candidate: Candidate,
    client: BaseAiClient,
    messages: list[Any],
    tools: list[Any],
    outcome: _TurnOutcome,
) -> None:
    """The tool loop with one model. Raises `AiProviderError` when a call
    fails; what was spent until then is already in `outcome` and recorded."""
    for round_index in range(MAX_TOOL_ROUNDTRIPS):
        started = time.monotonic()
        result: ChatTurnResult = await client.send(
            messages, tools, candidate.model.model_id, SYSTEM_PROMPT
        )
        outcome.input_tokens += result.input_tokens
        outcome.output_tokens += result.output_tokens
        await record_usage(
            db,
            user_id=owner.id,
            provider_kind=candidate.provider.kind,
            model_id=candidate.model.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            purpose=Purpose.ASSISTANT,
            label=conversation.title,
            duration_ms=int((time.monotonic() - started) * 1000),
            fallback=candidate.fallback,
        )

        if result.text:
            outcome.final_text = result.text

        if not result.tool_calls:
            outcome.turn_native.append(result.raw_assistant_message)
            return

        outputs: list[tuple[ToolCall, str]] = []
        for call in result.tool_calls:
            spec = TOOL_SPECS.get(call.name)
            if spec is not None and not spec.mutating:
                # Read-only: safe to execute here and now.
                outputs.append((call, await execute_read_only_tool(db, owner, call)))
                continue
            # Mutating (or unknown): recorded only. Permission check #2
            # happens inside build_pending_action, which records a
            # `denied` entry rather than dropping the call.
            entry = await build_pending_action(db, owner, call)
            outcome.pending_actions.append(entry)
            if entry.get("status") == "denied":
                feedback = str(entry.get("reason") or "Refused.")
            else:
                feedback = _PROPOSAL_FEEDBACK
            outputs.append((call, feedback))

        follow_up = client.build_tool_result_messages(result, outputs)
        messages.extend(follow_up)
        outcome.turn_native.extend(follow_up)

        if round_index == MAX_TOOL_ROUNDTRIPS - 1:
            logger.warning(
                "AI turn for conversation %s hit the %d tool round-trip cap",
                conversation.id,
                MAX_TOOL_ROUNDTRIPS,
            )


async def _run_turn(
    conversation_id: str, user_message: str | None = None, *, allow_tools: bool
) -> dict[str, Any]:
    """Run one provider turn and persist the assistant's reply.

    `user_message`, when given, means *this call* is responsible for
    recording that text as the turn's own user-role message (used by
    `summarize_tool_output`, whose "user" input is really tool output fed
    back to the model, not something a human typed and already on screen).
    When `None` (the interactive chat's own `run_ai_turn`), the caller
    (`app.web.routes.ai.post_message`) has already inserted the human's
    message itself — synchronously, before enqueueing this task — so the
    conversation page can show it immediately rather than waiting for a
    background task to get scheduled first. This function then just
    replays history, which already ends with that message.

    The conversation's own model answers; when it fails (a provider error
    or its timeout), the primary and then the fallback model of Settings →
    AI try in turn, from the start of the turn. Every turn is audit-logged
    (`ai.turn`) with the model that answered, its tokens and its time.
    """
    async with db_session.AsyncSessionLocal() as db:
        conversation = await db.get(AiConversation, uuid.UUID(conversation_id))
        if conversation is None:
            return {"ok": False, "error": "Conversation not found."}

        owner = await db.get(User, conversation.user_id)
        if owner is None or not owner.is_active:
            return {"ok": False, "error": "The conversation's owner is no longer active."}

        app_settings = await get_or_create_app_settings(db)
        # Before the provider call, never after — see app.ai.usage.
        limit_error = await check_within_limits(db, app_settings)
        if limit_error is not None:
            if user_message is not None:
                db.add(
                    AiMessage(
                        conversation_id=conversation.id,
                        role=AiMessageRole.USER,
                        content=user_message,
                        provider_native=None,
                    )
                )
                await db.commit()
            return await _persist_assistant_error(db, conversation.id, limit_error)

        chain = await _chain(db, conversation, app_settings)
        if not chain:
            return await _persist_assistant_error(
                db, conversation.id, "This conversation's AI provider is no longer enabled."
            )

        if user_message is not None:
            first_kind = chain[0].provider.kind
            try:
                native: list[Any] | None = [
                    build_client(chain[0].provider).build_user_message(user_message)
                ]
            except AiProviderError:
                native = None
            db.add(
                AiMessage(
                    conversation_id=conversation.id,
                    role=AiMessageRole.USER,
                    content=user_message,
                    provider_native=native,
                    provider_kind=first_kind,
                )
            )
            await db.commit()

        # Permission filter #1: re-derived from the owner's current role.
        tools = available_tools(owner) if allow_tools else []
        started = time.monotonic()
        failures: list[str] = []
        spent = _TurnOutcome()
        for candidate in chain:
            try:
                client = build_client(candidate.provider)
            except AiProviderError as exc:
                failures.append(f"{candidate.label}: {exc}")
                continue
            messages = await _load_history(db, conversation, candidate.provider.kind, client)
            outcome = _TurnOutcome()
            try:
                await _attempt(
                    db, owner, conversation, candidate, client, messages, tools, outcome
                )
            except AiProviderError as exc:
                failures.append(f"{candidate.label}: {exc}")
                spent.input_tokens += outcome.input_tokens
                spent.output_tokens += outcome.output_tokens
                continue
            outcome.input_tokens += spent.input_tokens
            outcome.output_tokens += spent.output_tokens
            return await _finish(
                db, owner, conversation, candidate, outcome, failures, started
            )

        error = "; ".join(failures)
        await log_event(
            db,
            actor=owner.username,
            action="ai.turn",
            summary=f'AI assistant failed to answer in "{conversation.title}"',
            outcome=AuditOutcome.FAILURE,
            target_type="ai_conversation",
            target_id=conversation.id,
            target_label=conversation.title,
            details={
                "models": [c.label for c in chain],
                "error": error[:1000],
                "tokens": spent.input_tokens + spent.output_tokens,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return await _persist_assistant_error(db, conversation.id, error)


async def _finish(
    db: Any,
    owner: User,
    conversation: AiConversation,
    candidate: Candidate,
    outcome: _TurnOutcome,
    failures: list[str],
    started: float,
) -> dict[str, Any]:
    assistant_message = AiMessage(
        conversation_id=conversation.id,
        role=AiMessageRole.ASSISTANT,
        content=outcome.final_text or _NO_TEXT_FALLBACK,
        provider_native=outcome.turn_native or None,
        provider_kind=candidate.provider.kind,
        pending_actions=outcome.pending_actions or None,
    )
    db.add(assistant_message)
    await db.commit()
    tokens = outcome.input_tokens + outcome.output_tokens
    duration_ms = int((time.monotonic() - started) * 1000)
    await log_event(
        db,
        actor=owner.username,
        action="ai.turn",
        summary=(
            f'AI assistant answered in "{conversation.title}" with {candidate.label}'
            + (" (fallback model)" if candidate.fallback else "")
            + f" — {tokens} tokens, {duration_ms / 1000:.1f} s"
        ),
        target_type="ai_conversation",
        target_id=conversation.id,
        target_label=conversation.title,
        details={
            "model": candidate.label,
            "fallback": candidate.fallback,
            "failed_first": failures,
            "tokens": tokens,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "duration_ms": duration_ms,
            "proposed_actions": len(outcome.pending_actions),
        },
    )
    return {
        "ok": True,
        "message_id": str(assistant_message.id),
        "pending_action_count": len(outcome.pending_actions),
    }


# A turn can make up to MAX_TOOL_ROUNDTRIPS (5) sequential provider calls,
# each with its provider's own timeout (90 s by default, at most
# `MAX_AI_TIMEOUT_SECONDS` — Settings → AI), and when one fails the turn
# starts over with the next model of the chain — worst case well over
# 450 s. Both tasks below need a `time_limit` comfortably above that, or
# Celery's own default (60s, see task_time_limit in app/tasks/celery_app.py)
# kills the task via SIGKILL mid-call, long before it would ever hit its
# own internal timeout — silently: nothing gets persisted, and the
# conversation just never receives a reply. This was a real bug, not a
# hypothetical one.
_AI_TURN_TIME_LIMIT_SECONDS = 1800


@celery_app.task(name="app.tasks.ai_jobs.run_ai_turn", time_limit=_AI_TURN_TIME_LIMIT_SECONDS)
def run_ai_turn(conversation_id: str) -> dict[str, Any]:
    """The interactive chat's own turn. This never receives the user's
    message text as an argument —
    `app.web.routes.ai.post_message` already persisted it (synchronously,
    before enqueueing this task) so the conversation page shows it
    immediately rather than waiting on a background task to even start."""
    return asyncio.run(_run_turn(conversation_id, allow_tools=True))
