"""Notification dispatch and delivery (email or webhook) — the "when X
happens, tell someone" pipeline behind Notifications
(`app/web/routes/notifications.py`, `app.db.models.notification_rule`).
See that model module's docstring for what a rule/template actually
holds; this module is the one place that reads them and turns a matching
event into an actual send.

Call `notify(db, event_type, fabric=..., context=...)` right after the
triggering fact is committed. Every failure here — no SMTP configured, no
matching rule, no recipient with an email set, the SMTP server itself
refusing the connection, a webhook URL that times out — is caught and
logged, never raised: a notification that fails to send must never break
the background job that triggered it, the same "best-effort, never
load-bearing" spirit `app.audit_syslog.forward_to_syslog` already has for
the audit log's own external mirror. Every attempt (success or failure)
is additionally recorded to `NotificationLog`
(`app.db.models.notification_log`) for the Notifications → History page —
`send_test_notification` (the "Send test" button on a rule's edit page)
writes there too, flagged `is_test=True`.

Current call sites (see `NotificationEventType`'s own docstring for how to
add another):
- `app.tasks.jobs._sync_fabric` — FABRIC_SYNC_FAILED, only on the
  healthy-to-failing transition, never on every sweep that confirms the
  same failure.
- `app.tasks.release_jobs._assess_release` — RELEASE_ASSESSED after every
  assessment, and UPDATE_BLOCKED per Fabric with a pending update to that
  release when its verdict turns STOP (not on every re-assessment that
  confirms it). ucc never runs updates, so UPDATE_RUN_SUCCEEDED/FAILED have
  no call site.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any, Protocol

import httpx2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import get_or_create_app_settings
from app.core.security import decrypt_secret
from app.db.models.app_settings import AppSettings, SmtpEncryption
from app.db.models.fabric import Fabric
from app.db.models.notification_log import (
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
    NotificationLog,
)
from app.db.models.notification_rule import (
    NotificationEventType,
    NotificationRule,
    NotificationTemplate,
)
from app.db.models.user import User
from app.i18n import DEFAULT_LOCALE_CODE

logger = logging.getLogger(__name__)

# Built-in subject/body used whenever no `NotificationTemplate` row
# overrides a given event type, keyed by locale code (same codes as
# `app.i18n` — currently "en"/"cs") — every `NotificationEventType` member
# needs an entry in every locale here. A locale with no entry falls back
# to `DEFAULT_LOCALE_CODE` (English), same "never worse than doing
# nothing" fallback `app.i18n.translate` itself uses. Which locale is
# used for a given recipient is `User.locale` (see `notify`'s "one email
# per recipient, rendered in *their* language" below) — an admin-edited
# `NotificationTemplate` override is a single value, not per-locale, since
# an admin who customizes the wording is expected to write it in
# whichever language they want every recipient to see it in.
_DEFAULT_TEMPLATES: dict[str, dict[NotificationEventType, tuple[str, str]]] = {
    "en": {
        NotificationEventType.FABRIC_SYNC_FAILED: (
            "ucc: inventory sync of {fabric_name} failed",
            "Syncing Fabric {fabric_name} from UniFi Site Manager failed at {timestamp}."
            "\n\n{details}",
        ),
        NotificationEventType.RELEASE_ASSESSED: (
            "ucc: {product} {version} assessed — risk {risk_level}/5",
            "The AI assessment of {product} {version} finished at {timestamp} with risk "
            "level {risk_level}/5.\n\n{details}",
        ),
        NotificationEventType.UPDATE_BLOCKED: (
            "ucc: update of {target_name} to {version} was stopped",
            "The update of {target_name} ({fabric_name}) to {version} was stopped at "
            "{timestamp}: risk level {risk_level}/5 is above the acceptable level "
            "{max_risk_level}/5. Someone with the update.override permission can allow "
            "it manually in ucc.\n\n{details}",
        ),
        NotificationEventType.RELEASE_RISK_CHANGED: (
            "ucc: {product} {version} is now risk {risk_level}/5 (was {previous_risk_level}/5)",
            "A re-assessment at {timestamp} changed the risk level of {product} {version} "
            "from {previous_risk_level}/5 to {risk_level}/5.\n\n{details}",
        ),
        NotificationEventType.INSTALLED_AT_RISK: (
            "ucc: {product} {version} running in {fabric_name} is now risk {risk_level}/5",
            "{product} {version}, which {count} console(s)/device(s) in {fabric_name} run "
            "now, was rated {risk_level}/5 at {timestamp} — above the acceptable level "
            "{max_risk_level}/5 for that hardware: {target_name}. Check whether a newer "
            "version is GO, or whether the problems affect you.\n\n{details}",
        ),
        NotificationEventType.AUTO_UPDATE_RISK: (
            "ucc: UniFi auto-update would install {version} on {target_name}",
            "{target_name} ({fabric_name}) has UniFi's own auto-update on ({schedule}) and "
            "would install {version}, rated {risk_level}/5 at {timestamp} — above the "
            "acceptable level {max_risk_level}/5. ucc can't stop it: turn the auto-update "
            "off in UniFi or accept the risk.\n\n{details}",
        ),
        NotificationEventType.UPDATE_RUN_FAILED: (
            "ucc: update of {target_name} failed",
            "The update of {target_name} ({fabric_name}) to {version} failed at "
            "{timestamp}.\n\n{details}",
        ),
        NotificationEventType.UPDATE_RUN_SUCCEEDED: (
            "ucc: {target_name} updated to {version}",
            "{target_name} ({fabric_name}) was updated to {version} at {timestamp}."
            "\n\n{details}",
        ),
    },
    "cs": {
        NotificationEventType.FABRIC_SYNC_FAILED: (
            "ucc: synchronizace {fabric_name} selhala",
            "Synchronizace Fabricu {fabric_name} z UniFi Site Manageru selhala v {timestamp}."
            "\n\n{details}",
        ),
        NotificationEventType.RELEASE_ASSESSED: (
            "ucc: {product} {version} ohodnoceno — riziko {risk_level}/5",
            "AI hodnocení {product} {version} doběhlo v {timestamp} s úrovní rizika "
            "{risk_level}/5.\n\n{details}",
        ),
        NotificationEventType.UPDATE_BLOCKED: (
            "ucc: aktualizace {target_name} na {version} byla zastavena",
            "Aktualizace {target_name} ({fabric_name}) na {version} byla v {timestamp} "
            "zastavena: úroveň rizika {risk_level}/5 je nad přijatelnou úrovní "
            "{max_risk_level}/5. Uživatel s oprávněním update.override ji může v ucc "
            "ručně povolit.\n\n{details}",
        ),
        NotificationEventType.RELEASE_RISK_CHANGED: (
            "ucc: {product} {version} má nově riziko {risk_level}/5 "
            "(dříve {previous_risk_level}/5)",
            "Přehodnocení v {timestamp} změnilo úroveň rizika {product} {version} "
            "z {previous_risk_level}/5 na {risk_level}/5.\n\n{details}",
        ),
        NotificationEventType.INSTALLED_AT_RISK: (
            "ucc: {product} {version} běžící ve {fabric_name} má nově riziko {risk_level}/5",
            "{product} {version}, které teď běží na {count} konzolích/zařízeních ve "
            "{fabric_name}, bylo v {timestamp} ohodnoceno {risk_level}/5 — nad přijatelnou "
            "úrovní {max_risk_level}/5 pro tento hardware: {target_name}. Zkontrolujte, jestli "
            "je novější verze GO, nebo jestli se vás problémy týkají.\n\n{details}",
        ),
        NotificationEventType.AUTO_UPDATE_RISK: (
            "ucc: automatická aktualizace UniFi by nainstalovala {version} na {target_name}",
            "{target_name} ({fabric_name}) má zapnutou vlastní automatickou aktualizaci UniFi "
            "({schedule}) a nainstalovala by {version}, ohodnocené v {timestamp} na "
            "{risk_level}/5 — nad přijatelnou úrovní {max_risk_level}/5. ucc ji nezastaví: "
            "vypněte automatickou aktualizaci v UniFi, nebo riziko přijměte.\n\n{details}",
        ),
        NotificationEventType.UPDATE_RUN_FAILED: (
            "ucc: aktualizace {target_name} selhala",
            "Aktualizace {target_name} ({fabric_name}) na {version} selhala v {timestamp}."
            "\n\n{details}",
        ),
        NotificationEventType.UPDATE_RUN_SUCCEEDED: (
            "ucc: {target_name} aktualizováno na {version}",
            "{target_name} ({fabric_name}) bylo v {timestamp} aktualizováno na {version}."
            "\n\n{details}",
        ),
    },
}


class _SafeDict(dict[str, str]):
    """Used with `str.format_map` so a placeholder an admin-edited template
    doesn't recognize (a typo, or a context key this event type doesn't
    provide) is left as literal text instead of raising `KeyError` and
    losing the whole notification."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def default_template(
    event_type: NotificationEventType, locale: str = DEFAULT_LOCALE_CODE
) -> tuple[str, str]:
    """The built-in (subject, body) for `event_type` in `locale`, used
    whenever no `NotificationTemplate` row overrides it — also what the
    Notifications → Templates page shows as the starting point to edit
    (in the viewing admin's own UI language — see
    `app/web/routes/notifications.py`), and what "Reset to default" puts
    back. A `locale` with no translations here falls back to English."""
    return _DEFAULT_TEMPLATES.get(locale, _DEFAULT_TEMPLATES[DEFAULT_LOCALE_CODE])[event_type]


class _TemplateLike(Protocol):
    """Structural type for `render_template`'s `template` argument — a
    `NotificationTemplate`, a `NotificationCustomTemplate`, or (only from
    `send_test_notification` below, for a rule with no custom template of
    its own) a throwaway `SimpleNamespace(subject=..., body=...)` all
    satisfy this without needing a real shared base class."""

    subject: str
    body: str


def render_template(
    event_type: NotificationEventType,
    template: _TemplateLike | None,
    context: dict[str, Any],
    *,
    locale: str = DEFAULT_LOCALE_CODE,
) -> tuple[str, str]:
    """Subject and body for one event, substituting `{placeholder}` values
    from `context` into the stored (or built-in default for `locale`, if
    `template` is None) text — plain `str.format_map`, not a template
    engine, so an admin-edited body can never execute code or reach
    outside its own string (see `NotificationTemplate`'s docstring).
    Missing placeholders are left as literal text rather than raising.
    `template` is either the per-event default/override or a rule's own
    `NotificationCustomTemplate` (see `NotificationRule.custom_template`) —
    both expose the same plain `subject`/`body` strings, so either renders
    identically here.

    Placeholders every event type provides: `{event}` (the event type's
    code, e.g. "update.blocked"), `{timestamp}` (UTC ISO-8601).
    Fabric-scoped events additionally provide `{fabric_name}`; update and
    release events provide `{target_name}`, `{product}`, `{version}`,
    `{risk_level}` and `{max_risk_level}` where they apply;
    `release.risk_changed` adds `{previous_risk_level}`,
    `release.installed_at_risk` `{count}` (consoles/devices running it) and
    `update.auto_update_risk` `{schedule}` (UniFi's auto-update schedule).
    `{details}` is
    free-form, event-specific text passed via `notify`'s `context`."""
    if template is not None:
        subject_tpl, body_tpl = template.subject, template.body
    else:
        subject_tpl, body_tpl = default_template(event_type, locale)
    safe_context = _SafeDict({k: "" if v is None else str(v) for k, v in context.items()})
    return subject_tpl.format_map(safe_context), body_tpl.format_map(safe_context)


def _rule_matches_scope(rule: NotificationRule, fabric: Fabric | None) -> bool:
    """Empty `fabrics` means "every Fabric" — see `NotificationRule`'s own
    docstring. An event with no Fabric at all matches every scope."""
    if fabric is None or not rule.fabrics:
        return True
    return any(f.id == fabric.id for f in rule.fabrics)


async def _matching_rules(
    db: AsyncSession, event_type: NotificationEventType, fabric: Fabric | None
) -> list[NotificationRule]:
    # Rules are admin-authored config, not inventory-scale data — loading every
    # enabled one and filtering in Python (rather than a JSON-contains query
    # that would need different SQL per database backend) is the simplest
    # correct thing, same reasoning `app.audit_syslog` gives for not
    # over-engineering a low-cardinality lookup.
    result = await db.execute(select(NotificationRule).where(NotificationRule.enabled.is_(True)))
    rules = list(result.scalars().all())
    return [
        rule
        for rule in rules
        if event_type in rule.event_type_enums and _rule_matches_scope(rule, fabric)
    ]


async def _recipients(db: AsyncSession, rules: list[NotificationRule]) -> list[User]:
    """The union of every matching rule's recipients — directly-listed
    users plus every active user holding one of its target roles —
    deduplicated by id, and silently dropping a disabled account or one
    with no `User.email` set (see `NotificationRule`'s docstring). Returns
    full `User` objects, not just addresses, so `notify` can render each
    one's email in *their* own `User.locale` (see this module's i18n note
    above)."""
    by_id: dict[object, User] = {u.id: u for u in (u for rule in rules for u in rule.users)}
    role_ids = {role.id for rule in rules for role in rule.roles}
    if role_ids:
        result = await db.execute(select(User).where(User.role_id.in_(role_ids)))
        for user in result.scalars().all():
            by_id[user.id] = user
    return [u for u in by_id.values() if u.is_active and u.email]


def _send_smtp_message(
    app_settings: AppSettings, to_address: str, subject: str, body: str
) -> None:
    """Synchronous SMTP send (stdlib `smtplib`) — run via `asyncio.to_thread`
    from `notify` below, the same "sync library, async caller" seam every
    Celery task in `app.tasks.jobs` already crosses, rather than adding an
    async SMTP client dependency for one feature. One connection per
    recipient — simple and correct at the small recipient counts a
    notification rule realistically has; reusing one connection for a
    multi-recipient send is a possible future optimization, not a
    correctness issue."""
    message = EmailMessage()
    message["Subject"] = subject
    from_name = app_settings.smtp_from_name or "ucc"
    from_address = app_settings.smtp_from_address or app_settings.smtp_username or to_address
    message["From"] = f"{from_name} <{from_address}>"
    message["To"] = to_address
    message.set_content(body)

    assert app_settings.smtp_host is not None  # checked by the caller (notify)
    connect: type[smtplib.SMTP] = (
        smtplib.SMTP_SSL
        if app_settings.smtp_encryption == SmtpEncryption.SSL_TLS
        else smtplib.SMTP
    )
    with connect(app_settings.smtp_host, app_settings.smtp_port, timeout=15) as client:
        if app_settings.smtp_encryption == SmtpEncryption.STARTTLS:
            client.starttls()
        if app_settings.smtp_username and app_settings.smtp_password_encrypted:
            client.login(
                app_settings.smtp_username, decrypt_secret(app_settings.smtp_password_encrypted)
            )
        client.send_message(message)


async def _send_webhook(
    url: str,
    event_type: str,
    rule_name: str,
    subject: str,
    body: str,
    context: dict[str, Any],
) -> tuple[NotificationDeliveryStatus, str | None]:
    """POST one JSON payload to a rule's `webhook_url`. No signature or
    bearer-auth scheme of its own — a URL with an embedded token/path
    secret (a common convention for webhook receivers — Slack incoming
    webhooks, Discord, a private endpoint) covers that; `url` is
    admin-authored config requiring `Permission.NOTIFICATION_MANAGE`, the
    same trust level `AppSettings`' other outbound integrations (SMTP
    relay, syslog forwarding) already have, not untrusted input — see
    wiki/Notifications.md. Returns `(status, error)` rather than raising;
    the caller logs and never lets a bad webhook break the triggering
    job."""
    payload = {
        "event": event_type,
        "rule_name": rule_name,
        "subject": subject,
        "body": body,
        "fabric_name": context.get("fabric_name"),
        "target_name": context.get("target_name"),
        "version": context.get("version"),
        "risk_level": context.get("risk_level"),
        "timestamp": context.get("timestamp"),
    }
    try:
        async with httpx2.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            return NotificationDeliveryStatus.FAILED, f"HTTP {response.status_code}"
        return NotificationDeliveryStatus.SENT, None
    except Exception as exc:  # noqa: BLE001 - logged as a delivery failure, never raised
        return NotificationDeliveryStatus.FAILED, str(exc)[:2000]


def _delivery_log(
    *,
    rule: NotificationRule | None,
    rule_name: str,
    event_type: str,
    channel: NotificationDeliveryChannel,
    target: str,
    fabric: Fabric | None,
    status: NotificationDeliveryStatus,
    error: str | None,
    is_test: bool = False,
) -> NotificationLog:
    return NotificationLog(
        rule_id=rule.id if rule else None,
        rule_name=rule_name,
        event_type=event_type,
        channel=channel.value,
        target=target,
        fabric_name=fabric.name if fabric else None,
        status=status.value,
        error=error,
        is_test=is_test,
    )


async def notify(
    db: AsyncSession,
    event_type: NotificationEventType,
    *,
    fabric: Fabric | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Fire `event_type` — find every enabled rule that lists it and whose
    scope includes `fabric` (or has no scope at all), and deliver
    through each matching rule's own channel: email to its own resolved
    recipients, or one webhook POST if it's set to that instead. A
    complete no-op when nothing matches; every failure along the way — no
    SMTP configured, no recipient has an email address, the SMTP server or
    the webhook endpoint refuses the connection — is caught, logged to
    `NotificationLog`, and never raised (see the module docstring).

    Rendered per *rule*, not once for the deduplicated recipient set across
    every matching rule — a rule can select its own `custom_template`
    (`NotificationRule.custom_template_id`), so two rules that both match
    this event for the same person are two legitimately different emails,
    not a duplicate to collapse. A rule with no `custom_template_id` uses
    the shared per-event default/override exactly as before."""
    try:
        app_settings = await get_or_create_app_settings(db)
        rules = await _matching_rules(db, event_type, fabric)
        if not rules:
            return

        default_template = (
            await db.execute(
                select(NotificationTemplate).where(
                    NotificationTemplate.event_type == event_type.value
                )
            )
        ).scalar_one_or_none()
        full_context: dict[str, Any] = dict(context or {})
        full_context.setdefault("event", event_type.value)
        full_context.setdefault("timestamp", datetime.now(UTC).isoformat())
        full_context.setdefault("details", "")
        if fabric is not None:
            full_context.setdefault("fabric_name", fabric.name)

        logs: list[NotificationLog] = []
        for rule in rules:
            rule_template = rule.custom_template if rule.custom_template_id else default_template

            if rule.delivery_channel == NotificationDeliveryChannel.WEBHOOK.value:
                if not rule.webhook_url:
                    continue
                subject, body = render_template(event_type, rule_template, full_context)
                status, error = await _send_webhook(
                    rule.webhook_url, event_type.value, rule.name, subject, body, full_context
                )
                if status is NotificationDeliveryStatus.FAILED:
                    logger.warning(
                        "Webhook delivery failed for rule=%s: %s", rule.name, error
                    )
                logs.append(
                    _delivery_log(
                        rule=rule,
                        rule_name=rule.name,
                        event_type=event_type.value,
                        channel=NotificationDeliveryChannel.WEBHOOK,
                        target=rule.webhook_url,
                        fabric=fabric,
                        status=status,
                        error=error,
                    )
                )
                continue

            if not app_settings.smtp_enabled or not app_settings.smtp_host:
                continue
            recipients = await _recipients(db, [rule])
            if not recipients:
                continue

            # Rendered once per recipient, in *their* own UI language
            # (`User.locale`, same field the rest of the app already uses
            # for this) rather than once for everyone — an admin-set
            # template is still a single value regardless of locale (see
            # the module-level note above `_DEFAULT_TEMPLATES`).
            for user in recipients:
                assert user.email is not None  # guaranteed by `_recipients`
                subject, body = render_template(
                    event_type,
                    rule_template,
                    full_context,
                    locale=user.locale or DEFAULT_LOCALE_CODE,
                )
                try:
                    await asyncio.to_thread(
                        _send_smtp_message, app_settings, user.email, subject, body
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to send notification email to %s", user.email, exc_info=True
                    )
                    logs.append(
                        _delivery_log(
                            rule=rule,
                            rule_name=rule.name,
                            event_type=event_type.value,
                            channel=NotificationDeliveryChannel.EMAIL,
                            target=user.email,
                            fabric=fabric,
                            status=NotificationDeliveryStatus.FAILED,
                            error=str(exc)[:2000],
                        )
                    )
                else:
                    logs.append(
                        _delivery_log(
                            rule=rule,
                            rule_name=rule.name,
                            event_type=event_type.value,
                            channel=NotificationDeliveryChannel.EMAIL,
                            target=user.email,
                            fabric=fabric,
                            status=NotificationDeliveryStatus.SENT,
                            error=None,
                        )
                    )
        if logs:
            db.add_all(logs)
            await db.commit()
    except Exception:
        logger.warning(
            "Notification dispatch failed for event=%s", event_type.value, exc_info=True
        )


_TEST_MESSAGE = {
    "en": (
        "[TEST] ucc notification test",
        'This is a test of rule "{rule_name}" — if you received this, delivery is working.',
    ),
    "cs": (
        "[TEST] Testovací notifikace ucc",
        'Toto je test pravidla "{rule_name}" — pokud jste ji obdrželi, doručování funguje.',
    ),
}


async def send_test_notification(
    db: AsyncSession,
    rule: NotificationRule,
    *,
    to_email: str | None,
    locale: str = DEFAULT_LOCALE_CODE,
) -> tuple[bool, str | None]:
    """Send one synthetic test notification through `rule`'s own configured
    channel/template — the "Send test" button on its edit page
    (`app/web/routes/notifications.py`). Ignores the rule's real
    recipients/scope entirely: for email, goes only to `to_email` (the
    admin clicking the button, never the rule's actual audience, so
    testing never spams real recipients); for webhook, POSTs to the
    rule's own `webhook_url`, same as a real event would. Returns
    `(ok, error)` so the route can show an inline result instead of a
    generic "check the history page" — logged to `NotificationLog` either
    way (`is_test=True`), so a test send shows up in the delivery history
    but is clearly marked apart from real ones."""
    subject_tpl, body_tpl = _TEST_MESSAGE.get(locale, _TEST_MESSAGE[DEFAULT_LOCALE_CODE])
    context = {
        "event": "test",
        "timestamp": datetime.now(UTC).isoformat(),
        "details": "",
        "fabric_name": "test-fabric",
        "target_name": "test-device",
        "version": "0.0.0",
        "rule_name": rule.name,
    }
    template = (
        rule.custom_template
        if rule.custom_template_id
        else SimpleNamespace(subject=subject_tpl, body=body_tpl)
    )
    subject, body = render_template(NotificationEventType.UPDATE_RUN_SUCCEEDED, template, context)
    if not rule.custom_template_id:
        subject = f"[TEST] {subject}" if not subject.startswith("[TEST]") else subject

    if rule.delivery_channel == NotificationDeliveryChannel.WEBHOOK.value:
        if not rule.webhook_url:
            return False, "No webhook URL configured on this rule."
        status, error = await _send_webhook(
            rule.webhook_url, "test", rule.name, subject, body, context
        )
        db.add(
            _delivery_log(
                rule=rule,
                rule_name=rule.name,
                event_type="test",
                channel=NotificationDeliveryChannel.WEBHOOK,
                target=rule.webhook_url,
                fabric=None,
                status=status,
                error=error,
                is_test=True,
            )
        )
        await db.commit()
        return status is NotificationDeliveryStatus.SENT, error

    if not to_email:
        return False, "Your account has no email address set."
    app_settings = await get_or_create_app_settings(db)
    if not app_settings.smtp_enabled or not app_settings.smtp_host:
        return False, "SMTP isn't configured/enabled in Settings."
    try:
        await asyncio.to_thread(_send_smtp_message, app_settings, to_email, subject, body)
    except Exception as exc:
        db.add(
            _delivery_log(
                rule=rule,
                rule_name=rule.name,
                event_type="test",
                channel=NotificationDeliveryChannel.EMAIL,
                target=to_email,
                fabric=None,
                status=NotificationDeliveryStatus.FAILED,
                error=str(exc)[:2000],
                is_test=True,
            )
        )
        await db.commit()
        return False, str(exc)
    db.add(
        _delivery_log(
            rule=rule,
            rule_name=rule.name,
            event_type="test",
            channel=NotificationDeliveryChannel.EMAIL,
            target=to_email,
            fabric=None,
            status=NotificationDeliveryStatus.SENT,
            error=None,
            is_test=True,
        )
    )
    await db.commit()
    return True, None
