"""Settings — the inventory sync interval and the notification-log
retention (Checks & retention), release assessment (Releases: acceptable
risk level, the AI model, Reddit and web search sources), the audit log's
own hash-chain verification (Security), and
the LDAP/OIDC/syslog/SMTP login and integration configuration
(Integrations) — see `app/db/models/app_settings.py` for why all of this
is Settings-page config rather than environment variables.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AiProviderError
from app.ai.config import (
    get_or_create_ai_provider_configs,
    get_selectable_models,
    replace_fetched_models,
)
from app.ai.providers import MAX_AI_TIMEOUT_SECONDS, MIN_AI_TIMEOUT_SECONDS, build_client
from app.ai.usage import usage_summary
from app.audit import log_event, verify_chain
from app.auth.dependencies import require_permission
from app.core.app_settings import get_or_create_app_settings
from app.core.config import get_settings
from app.core.csrf import get_or_create_csrf_token, set_csrf_cookie, verify_csrf
from app.core.security import encrypt_secret
from app.db.models.ai_model import AiModel
from app.db.models.ai_provider import AiProviderConfig, AiProviderKind
from app.db.models.app_settings import (
    DEFAULT_LDAP_USER_SEARCH_FILTER,
    DEFAULT_OIDC_SCOPES,
    DEFAULT_OIDC_USERNAME_CLAIM,
    DEFAULT_SMTP_PORT,
    DEFAULT_SYSLOG_PORT,
    SmtpEncryption,
    SyslogProtocol,
    WebSearchKind,
)
from app.db.models.audit_log import AuditOutcome
from app.db.models.role import Permission
from app.db.session import get_db
from app.releases.policy import global_policy, stages_from_form
from app.releases.sources.web import validate_searxng_url
from app.web.templating import t, templates

router = APIRouter(
    prefix="/settings", dependencies=[Depends(require_permission(Permission.SETTINGS_VIEW))]
)
_manage = Depends(require_permission(Permission.SETTINGS_MANAGE))

# Settings is split into tabs (see `partials/_tabnav.html`), with only ever
# one GET route rather than one per tab: every POST handler below redirects back to `/settings`
# regardless of which tab it belongs to, and giving each tab its own route
# would mean every one of those redirects needs to know which page it's
# redirecting *from* just to send you back to the right place. A `tab` query
# string param is simpler and gets the same result — every handler below
# just needs to know which tab *it itself* belongs to, to redirect back to
# `/settings?tab=<that tab>` instead of losing your place on every save.
_TAB_KEYS = ["checks", "releases", "security", "integrations", "ai"]
_VALID_TABS = set(_TAB_KEYS)
_DEFAULT_TAB = "checks"


def _tabs(request: Request) -> list[tuple[str, str, str]]:
    """The (key, label, url) tabs rendered by `partials/_tabnav.html` —
    labels resolved per-request so they follow the viewer's own locale."""
    return [
        (key, t(request, f"settings.tab.{key}"), f"/settings?tab={key}") for key in _TAB_KEYS
    ]


def _normalize_tab(tab: str) -> str:
    """An unrecognized/missing `tab` value (a stale bookmark, a typo'd URL)
    falls back to the first tab rather than 404ing or rendering no tab's
    content at all."""
    return tab if tab in _VALID_TABS else _DEFAULT_TAB


async def _render_settings(
    request: Request,
    db: AsyncSession,
    errors: list[str],
    *,
    tab: str = _DEFAULT_TAB,
    **extra: object,
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    # Creates the five AI provider rows on first visit — same lazy
    # singleton-row idea as `get_or_create_app_settings` above.
    ai_configs = await get_or_create_ai_provider_configs(db)
    csrf_token, new_cookie = get_or_create_csrf_token(request)
    context: dict[str, object] = {
        "settings": get_settings(),
        "app_settings": app_settings,
        "csrf_token": csrf_token,
        "errors": errors,
        "syslog_protocols": list(SyslogProtocol),
        "smtp_encryptions": list(SmtpEncryption),
        # Ordered by the enum, so the panel always renders the same five
        # blocks in the same order.
        "ai_configs": [ai_configs[kind] for kind in AiProviderKind if kind in ai_configs],
        "openai_compatible_kind": AiProviderKind.OPENAI_COMPATIBLE.value,
        "selectable_models": await get_selectable_models(db),
        "ai_usage": await usage_summary(db),
        "global_policy": global_policy(app_settings),
        "ai_timeout_range": (MIN_AI_TIMEOUT_SECONDS, MAX_AI_TIMEOUT_SECONDS),
        "web_search_kinds": list(WebSearchKind),
        "tabs": _tabs(request),
        "active_tab": tab,
        **extra,
    }
    response = templates.TemplateResponse(request, "settings/index.html", context)
    if new_cookie:
        set_csrf_cookie(response, new_cookie)
    return response


@router.get("")
async def show_settings(
    request: Request, db: AsyncSession = Depends(get_db), tab: str = _DEFAULT_TAB
) -> Response:
    return await _render_settings(request, db, [], tab=_normalize_tab(tab))


def _parse_retention_days(raw: str) -> tuple[int | None, str | None]:
    """Shared by the retention handlers below — every one means the same
    thing (empty = keep forever, otherwise a non-negative whole number of
    days). Returns `(days, error_message)`; exactly one is `None`."""
    stripped = raw.strip()
    if stripped == "":
        return None, None
    try:
        value = int(stripped)
        if value < 0:
            raise ValueError("must not be negative")
    except ValueError:
        return None, f'"{stripped}" isn\'t a whole number of days (0 or more).'
    return value, None


def _parse_bounded_int(
    raw: str, *, label: str, minimum: int, maximum: int
) -> tuple[int | None, str | None]:
    """Shared by `update_background_checks` below — every field there is a
    whole number of seconds with a sane range, unlike the
    retention fields above (which allow an empty "forever"). Returns
    `(value, error_message)`; exactly one is `None`."""
    stripped = raw.strip()
    try:
        value = int(stripped)
    except ValueError:
        return None, f'"{stripped}" isn\'t a whole number for {label}.'
    if not (minimum <= value <= maximum):
        return None, f"{label} must be between {minimum} and {maximum}."
    return value, None


@router.post("/background-checks", dependencies=[_manage, Depends(verify_csrf)])
async def update_background_checks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    inventory_sync_interval_seconds: str = Form(""),
    release_catalog_interval_hours: str = Form(""),
) -> Response:
    """The inventory sync and Releases catalogue intervals. Only read by
    Celery Beat at its own process start (`app.tasks.celery_app`), so a change needs a restart of
    the beat service — see `settings.checks.background_checks_hint`.

    The lower bound keeps a sweep well inside Site Manager's rate limits
    even with many Fabrics."""
    app_settings = await get_or_create_app_settings(db)
    value, error = _parse_bounded_int(
        inventory_sync_interval_seconds,
        label="Inventory sync interval",
        minimum=60,
        maximum=86400,
    )
    if error or value is None:
        return await _render_settings(request, db, [error or "Invalid value."], tab="checks")
    # Optional, so a form from before 0.2.3 still saves.
    hours = app_settings.release_catalog_interval_hours
    if release_catalog_interval_hours.strip():
        parsed, error = _parse_bounded_int(
            release_catalog_interval_hours,
            label="Releases catalogue refresh interval",
            minimum=1,
            maximum=168,
        )
        if error or parsed is None:
            return await _render_settings(request, db, [error or "Invalid value."], tab="checks")
        hours = parsed

    app_settings.inventory_sync_interval_seconds = value
    app_settings.release_catalog_interval_hours = hours
    await db.commit()
    await log_event(
        db,
        request=request,
        action="settings.background_checks.update",
        summary=(
            f"Set the inventory sync interval to {value}s "
            f"and the catalogue refresh to {hours}h"
        ),
        details={"inventory_sync_interval_seconds": value, "release_catalog_interval_hours": hours},
    )
    return RedirectResponse(url="/settings?tab=checks", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/audit-retention", dependencies=[_manage, Depends(verify_csrf)])
async def update_audit_retention(
    request: Request,
    db: AsyncSession = Depends(get_db),
    retention_days: str = Form(""),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    new_value, error = _parse_retention_days(retention_days)
    if error:
        return await _render_settings(request, db, [error], tab="security")

    app_settings.audit_log_retention_days = new_value
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.audit_retention.update",
        summary=(
            f"Set audit log retention to {new_value} day(s)"
            if new_value is not None
            else "Set audit log retention to keep forever"
        ),
    )

    return RedirectResponse(url="/settings?tab=security", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/notification-log-retention", dependencies=[_manage, Depends(verify_csrf)])
async def update_notification_log_retention(
    request: Request,
    db: AsyncSession = Depends(get_db),
    retention_days: str = Form(""),
) -> Response:
    """Same shape as `update_audit_retention`
    above, for `NotificationLog` rows (delivery history — one row per
    actual send attempt) — see `app.tasks.jobs.purge_old_notification_logs`."""
    app_settings = await get_or_create_app_settings(db)
    new_value, error = _parse_retention_days(retention_days)
    if error:
        return await _render_settings(request, db, [error], tab="checks")

    app_settings.notification_log_retention_days = new_value
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.notification_log_retention.update",
        summary=(
            f"Set notification log retention to {new_value} day(s)"
            if new_value is not None
            else "Set notification log retention to keep forever"
        ),
    )

    return RedirectResponse(url="/settings?tab=checks", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/audit-verify", dependencies=[_manage, Depends(verify_csrf)])
async def verify_audit_chain(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Recompute the audit log's hash chain on demand — see
    `app.audit.verify_chain`. Result isn't stored anywhere; it's only ever
    the answer to "is the trail intact right now."""
    result = await verify_chain(db)
    await log_event(
        db,
        request=request,
        action="audit_log.verify",
        summary=f"Verified audit log hash chain: {result.message}",
        outcome=AuditOutcome.SUCCESS if result.ok else AuditOutcome.FAILURE,
        details={"checked": result.checked, "broken_at_sequence": result.broken_at_sequence},
    )
    return await _render_settings(request, db, [], tab="security", verify_result=result)


@router.post("/ldap", dependencies=[_manage, Depends(verify_csrf)])
async def update_ldap_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    ldap_enabled: str = Form(""),
    ldap_server_uri: str = Form(""),
    ldap_use_starttls: str = Form(""),
    ldap_tls_verify: str = Form(""),
    ldap_bind_dn: str = Form(""),
    # Blank = keep the existing bind password unchanged.
    ldap_bind_password: str = Form(""),
    ldap_user_search_base: str = Form(""),
    ldap_user_search_filter: str = Form(""),
    ldap_connect_timeout_seconds: str = Form("5"),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []

    server_uri = ldap_server_uri.strip()
    if server_uri and not (server_uri.startswith("ldap://") or server_uri.startswith("ldaps://")):
        errors.append('Server URI must start with "ldap://" or "ldaps://".')

    try:
        timeout = int(ldap_connect_timeout_seconds.strip() or "5")
        if timeout <= 0:
            raise ValueError
    except ValueError:
        errors.append("Connect timeout must be a positive whole number of seconds.")
        timeout = app_settings.ldap_connect_timeout_seconds

    search_filter = ldap_user_search_filter.strip() or DEFAULT_LDAP_USER_SEARCH_FILTER
    if "{username}" not in search_filter:
        errors.append('Search filter must contain "{username}".')

    if bool(ldap_enabled) and not (
        server_uri and ldap_bind_dn.strip() and ldap_user_search_base.strip()
    ):
        errors.append("Enabling LDAP needs at least a server URI, bind DN, and search base.")

    if errors:
        return await _render_settings(request, db, errors, tab="integrations")

    app_settings.ldap_enabled = bool(ldap_enabled)
    app_settings.ldap_server_uri = server_uri or None
    app_settings.ldap_use_starttls = bool(ldap_use_starttls)
    app_settings.ldap_tls_verify = bool(ldap_tls_verify)
    app_settings.ldap_bind_dn = ldap_bind_dn.strip() or None
    if ldap_bind_password:
        app_settings.ldap_bind_password_encrypted = encrypt_secret(ldap_bind_password)
    app_settings.ldap_user_search_base = ldap_user_search_base.strip() or None
    app_settings.ldap_user_search_filter = search_filter
    app_settings.ldap_connect_timeout_seconds = timeout
    await db.commit()

    summary = f"Updated LDAP settings ({'enabled' if app_settings.ldap_enabled else 'disabled'})"
    if not app_settings.ldap_tls_verify:
        summary += " — certificate verification is OFF"
    await log_event(db, request=request, action="settings.ldap.update", summary=summary)
    return RedirectResponse(url="/settings?tab=integrations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/oidc", dependencies=[_manage, Depends(verify_csrf)])
async def update_oidc_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    oidc_enabled: str = Form(""),
    oidc_provider_name: str = Form(""),
    oidc_issuer_url: str = Form(""),
    oidc_client_id: str = Form(""),
    # Blank = keep the existing client secret unchanged.
    oidc_client_secret: str = Form(""),
    oidc_username_claim: str = Form(""),
    oidc_scopes: str = Form(""),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []

    issuer_url = oidc_issuer_url.strip()
    if issuer_url and not (issuer_url.startswith("http://") or issuer_url.startswith("https://")):
        errors.append('Issuer URL must start with "http://" or "https://".')

    claim = oidc_username_claim.strip() or DEFAULT_OIDC_USERNAME_CLAIM
    scopes = oidc_scopes.strip() or DEFAULT_OIDC_SCOPES
    if "openid" not in scopes.split():
        errors.append('Scopes must include "openid".')

    if bool(oidc_enabled) and not (issuer_url and oidc_client_id.strip()):
        errors.append("Enabling OIDC needs at least an issuer URL and client ID.")

    if errors:
        return await _render_settings(request, db, errors, tab="integrations")

    app_settings.oidc_enabled = bool(oidc_enabled)
    app_settings.oidc_provider_name = oidc_provider_name.strip() or None
    app_settings.oidc_issuer_url = issuer_url or None
    app_settings.oidc_client_id = oidc_client_id.strip() or None
    if oidc_client_secret:
        app_settings.oidc_client_secret_encrypted = encrypt_secret(oidc_client_secret)
    app_settings.oidc_username_claim = claim
    app_settings.oidc_scopes = scopes
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.oidc.update",
        summary=f"Updated OIDC settings ({'enabled' if app_settings.oidc_enabled else 'disabled'})",
    )
    return RedirectResponse(url="/settings?tab=integrations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/syslog", dependencies=[_manage, Depends(verify_csrf)])
async def update_syslog_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    syslog_enabled: str = Form(""),
    syslog_host: str = Form(""),
    syslog_port: str = Form(str(DEFAULT_SYSLOG_PORT)),
    syslog_protocol: str = Form(SyslogProtocol.UDP.value),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []

    host = syslog_host.strip()
    try:
        protocol = SyslogProtocol(syslog_protocol)
    except ValueError:
        errors.append("Unknown syslog protocol.")
        protocol = app_settings.syslog_protocol

    try:
        port = int(syslog_port.strip() or str(DEFAULT_SYSLOG_PORT))
        if not (0 < port <= 65535):
            raise ValueError
    except ValueError:
        errors.append("Port must be a whole number between 1 and 65535.")
        port = app_settings.syslog_port

    if bool(syslog_enabled) and not host:
        errors.append("Enabling syslog forwarding needs a server host/IP.")

    if errors:
        return await _render_settings(request, db, errors, tab="integrations")

    app_settings.syslog_enabled = bool(syslog_enabled)
    app_settings.syslog_host = host or None
    app_settings.syslog_port = port
    app_settings.syslog_protocol = protocol
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.syslog.update",
        summary=(
            f"Updated syslog forwarding settings "
            f"({'enabled, ' + protocol.value if app_settings.syslog_enabled else 'disabled'})"
        ),
    )
    return RedirectResponse(url="/settings?tab=integrations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/smtp", dependencies=[_manage, Depends(verify_csrf)])
async def update_smtp_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    smtp_enabled: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(str(DEFAULT_SMTP_PORT)),
    smtp_encryption: str = Form(SmtpEncryption.STARTTLS.value),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_address: str = Form(""),
    smtp_from_name: str = Form(""),
) -> Response:
    """Configuration only, for now — nothing sends an email through this
    yet (see `AppSettings.smtp_*`'s own comment). Same write-only-secret
    convention as LDAP/OIDC above: a blank password field keeps the stored
    value."""
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []

    host = smtp_host.strip()
    try:
        encryption = SmtpEncryption(smtp_encryption)
    except ValueError:
        errors.append("Unknown SMTP encryption mode.")
        encryption = app_settings.smtp_encryption

    try:
        port = int(smtp_port.strip() or str(DEFAULT_SMTP_PORT))
        if not (0 < port <= 65535):
            raise ValueError
    except ValueError:
        errors.append("Port must be a whole number between 1 and 65535.")
        port = app_settings.smtp_port

    from_address = smtp_from_address.strip()
    if from_address and "@" not in from_address:
        errors.append('"From" address must be a valid email address.')

    if bool(smtp_enabled) and not host:
        errors.append("Enabling the SMTP relay needs a server host/IP.")

    if errors:
        return await _render_settings(request, db, errors, tab="integrations")

    app_settings.smtp_enabled = bool(smtp_enabled)
    app_settings.smtp_host = host or None
    app_settings.smtp_port = port
    app_settings.smtp_encryption = encryption
    app_settings.smtp_username = smtp_username.strip() or None
    if smtp_password:
        app_settings.smtp_password_encrypted = encrypt_secret(smtp_password)
    app_settings.smtp_from_address = from_address or None
    app_settings.smtp_from_name = smtp_from_name.strip() or None
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.smtp.update",
        summary=(
            f"Updated SMTP relay settings "
            f"({'enabled, ' + encryption.value if app_settings.smtp_enabled else 'disabled'})"
        ),
    )
    return RedirectResponse(url="/settings?tab=integrations", status_code=status.HTTP_303_SEE_OTHER)


# --- Release assessment (app/releases/) ---------------------------------------
#
# The acceptable risk level, which model rates releases, and the optional
# Reddit / web search sources. Same write-only-secret convention as above:
# a blank secret field keeps the stored value; the explicit "clear"
# checkbox removes it.


@router.post("/releases", dependencies=[_manage, Depends(verify_csrf)])
async def update_release_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    max_acceptable_risk_level: str = Form(""),
    # Moved to Settings → AI in 0.4.0 ("primary model"); still accepted
    # here when a form sends it, and left alone when it doesn't.
    release_assessment_model_id: str | None = Form(None),
    reddit_client_id: str = Form(""),
    reddit_client_secret: str = Form(""),
    reddit_clear: str = Form(""),
    web_search_kind: str = Form(WebSearchKind.NONE.value),
    web_search_url: str = Form(""),
    web_search_api_key: str = Form(""),
    web_search_clear_key: str = Form(""),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []

    level, error = _parse_bounded_int(
        max_acceptable_risk_level, label="Acceptable risk level", minimum=1, maximum=5
    )
    if error or level is None:
        errors.append(error or "Acceptable risk level must be 1 to 5.")

    model_id: uuid.UUID | None = app_settings.release_assessment_model_id
    model_label = "unchanged"
    if release_assessment_model_id is not None and not release_assessment_model_id.strip():
        model_id, model_label = None, "off"
    elif release_assessment_model_id is not None:
        selectable = {
            str(model.id): (config, model) for config, model in await get_selectable_models(db)
        }
        chosen = selectable.get(release_assessment_model_id.strip())
        if chosen is None:
            errors.append("Choose one of the enabled AI models.")
        else:
            model_id = chosen[1].id
            model_label = f"{chosen[0].kind.value}/{chosen[1].model_id}"

    try:
        kind = WebSearchKind(web_search_kind)
    except ValueError:
        errors.append("Unknown web search provider.")
        kind = WebSearchKind.NONE
    url = web_search_url.strip()
    if kind is WebSearchKind.SEARXNG:
        if not url:
            errors.append("SearXNG needs its URL.")
        else:
            url_error = validate_searxng_url(url)
            if url_error:
                errors.append(url_error)
    if (
        kind is WebSearchKind.BRAVE
        and not web_search_api_key.strip()
        and (web_search_clear_key or not app_settings.web_search_api_key_encrypted)
    ):
        errors.append("Brave Search needs an API key.")
    client_id = reddit_client_id.strip()
    if not reddit_clear and client_id and not (
        reddit_client_secret.strip() or app_settings.reddit_client_secret_encrypted
    ):
        errors.append("Reddit needs both the app's client id and its secret.")

    if errors:
        return await _render_settings(request, db, errors, tab="releases")

    assert level is not None
    raw_form = {key: str(value) for key, value in (await request.form()).items()}
    hidden = stages_from_form(raw_form, inherit_allowed=False) or []
    app_settings.max_acceptable_risk_level = level
    app_settings.hidden_release_stages = hidden
    app_settings.release_assessment_model_id = model_id
    if reddit_clear:
        app_settings.reddit_client_id = None
        app_settings.reddit_client_secret_encrypted = None
    else:
        app_settings.reddit_client_id = client_id or None
        if reddit_client_secret.strip():
            app_settings.reddit_client_secret_encrypted = encrypt_secret(
                reddit_client_secret.strip()
            )
        if not client_id:
            app_settings.reddit_client_secret_encrypted = None
    app_settings.web_search_kind = kind.value
    app_settings.web_search_url = url or None
    if web_search_clear_key:
        app_settings.web_search_api_key_encrypted = None
    elif web_search_api_key.strip():
        app_settings.web_search_api_key_encrypted = encrypt_secret(web_search_api_key.strip())
    await db.commit()

    reddit_on = bool(app_settings.reddit_client_id and app_settings.reddit_client_secret_encrypted)
    await log_event(
        db,
        request=request,
        action="settings.releases.update",
        summary=(
            f"Updated release assessment settings (acceptable level {level}/5, "
            f"model {model_label}, Reddit {'on' if reddit_on else 'off'}, "
            f"web search {kind.value})"
        ),
        details={
            "max_acceptable_risk_level": level,
            "hidden_release_stages": hidden,
            "model": model_label,
            "reddit": reddit_on,
            "web_search": kind.value,
        },
    )
    return RedirectResponse(url="/settings?tab=releases", status_code=status.HTTP_303_SEE_OTHER)


# --- AI assistant (app/ai/) --------------------------------------------------
#
# Everything below is web-UI-only and `settings.manage`-gated, like LDAP/OIDC.
# The API key is write-only from the browser's point of view: a blank field
# means "keep the stored value", and the stored value is never sent back to
# the page in any form — only the fact that one exists.


async def _get_provider_config(db: AsyncSession, kind_value: str) -> AiProviderConfig | None:
    try:
        kind = AiProviderKind(kind_value)
    except ValueError:
        return None
    configs = await get_or_create_ai_provider_configs(db)
    return configs.get(kind)


@router.post("/ai/provider", dependencies=[_manage, Depends(verify_csrf)])
async def update_ai_provider(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kind: str = Form(""),
    enabled: str = Form(""),
    # Blank = keep the existing API key unchanged, same convention as
    # `oidc_client_secret` / `ldap_bind_password`.
    api_key: str = Form(""),
    base_url: str = Form(""),
    timeout_seconds: str = Form(""),
) -> Response:
    config = await _get_provider_config(db, kind)
    if config is None:
        return await _render_settings(
            request, db, [f'Unknown AI provider "{kind}".'], tab="ai"
        )

    errors: list[str] = []
    url = base_url.strip()
    if config.kind == AiProviderKind.OPENAI_COMPATIBLE:
        if url and not (url.startswith("http://") or url.startswith("https://")):
            errors.append('The base URL must start with "http://" or "https://".')
        if bool(enabled) and not url:
            errors.append("Enabling an OpenAI-compatible provider needs a base URL.")
    if bool(enabled) and not api_key and config.api_key_encrypted is None:
        # OpenRouter can list models without a key, but a chat turn always
        # needs one — so enabling any provider requires one to be stored.
        errors.append("Enabling a provider needs an API key.")

    timeout = config.timeout_seconds
    if timeout_seconds.strip():
        parsed, timeout_error = _parse_bounded_int(
            timeout_seconds,
            label="Timeout",
            minimum=MIN_AI_TIMEOUT_SECONDS,
            maximum=MAX_AI_TIMEOUT_SECONDS,
        )
        if timeout_error or parsed is None:
            errors.append(timeout_error or "Timeout must be a whole number of seconds.")
        else:
            timeout = parsed

    if errors:
        return await _render_settings(request, db, errors, tab="ai")

    config.enabled = bool(enabled)
    config.timeout_seconds = timeout
    if api_key:
        config.api_key_encrypted = encrypt_secret(api_key)
    if config.kind == AiProviderKind.OPENAI_COMPATIBLE:
        config.base_url = url or None
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.ai_provider.update",
        summary=(
            f"Updated AI provider {config.kind.value} "
            f"({'enabled' if config.enabled else 'disabled'})"
        ),
        # Deliberately no key material, not even a length or a prefix.
        details={
            "provider": config.kind.value,
            "enabled": config.enabled,
            "timeout_seconds": config.timeout_seconds,
        },
    )
    return RedirectResponse(url="/settings?tab=ai", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ai/model-choice", dependencies=[_manage, Depends(verify_csrf)])
async def update_ai_model_choice(
    request: Request,
    db: AsyncSession = Depends(get_db),
    primary_model_id: str = Form(""),
    fallback_model_id: str = Form(""),
) -> Response:
    """The primary model (release assessments, the assistant's default)
    and the fallback tried when it fails — any enabled model of any
    enabled provider (`app.ai.fallback`)."""
    app_settings = await get_or_create_app_settings(db)
    selectable = {
        str(model.id): (config, model) for config, model in await get_selectable_models(db)
    }
    errors: list[str] = []
    chosen: dict[str, tuple[uuid.UUID | None, str]] = {}
    for name, raw in (("primary", primary_model_id), ("fallback", fallback_model_id)):
        if not raw.strip():
            chosen[name] = (None, "off")
            continue
        pair = selectable.get(raw.strip())
        if pair is None:
            errors.append("Choose one of the enabled AI models.")
            chosen[name] = (None, "off")
        else:
            chosen[name] = (pair[1].id, f"{pair[0].kind.value}/{pair[1].model_id}")
    if not errors and chosen["fallback"][0] is not None and (
        chosen["fallback"][0] == chosen["primary"][0]
    ):
        errors.append("The fallback model must differ from the primary one.")
    if errors:
        return await _render_settings(request, db, errors, tab="ai")

    app_settings.release_assessment_model_id = chosen["primary"][0]
    app_settings.ai_fallback_model_id = chosen["fallback"][0]
    await db.commit()
    await log_event(
        db,
        request=request,
        action="settings.ai_models.choice",
        summary=(
            f"Set the primary AI model to {chosen['primary'][1]} "
            f"and the fallback to {chosen['fallback'][1]}"
        ),
        details={"primary": chosen["primary"][1], "fallback": chosen["fallback"][1]},
    )
    return RedirectResponse(url="/settings?tab=ai", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ai/fetch-models", dependencies=[_manage, Depends(verify_csrf)])
async def fetch_ai_models(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kind: str = Form(""),
) -> Response:
    """Call the provider's list-models endpoint synchronously.

    Not a Celery task, unlike the chat turn: this is an admin-only action on
    an admin-only page that makes exactly one HTTP request with a 15-second
    timeout. Blocking the request for that is fine, and pushing it through
    the queue would only add a round trip and a result-polling dance for no
    benefit.
    """
    config = await _get_provider_config(db, kind)
    if config is None:
        return await _render_settings(
            request, db, [f'Unknown AI provider "{kind}".'], tab="ai"
        )

    try:
        client = build_client(config)
        models = await client.list_models()
    except AiProviderError as exc:
        await log_event(
            db,
            request=request,
            action="settings.ai_provider.fetch_models",
            summary=f"Failed to fetch models for AI provider {config.kind.value}",
            outcome=AuditOutcome.FAILURE,
            details={"provider": config.kind.value, "error": str(exc)},
        )
        return await _render_settings(request, db, [str(exc)], tab="ai")

    kept, removed = await replace_fetched_models(db, config, models)
    await log_event(
        db,
        request=request,
        action="settings.ai_provider.fetch_models",
        summary=f"Fetched {kept} model(s) for AI provider {config.kind.value}",
        details={"provider": config.kind.value, "fetched": kept, "removed": removed},
    )
    return RedirectResponse(url="/settings?tab=ai", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ai/models", dependencies=[_manage, Depends(verify_csrf)])
async def update_ai_models(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kind: str = Form(""),
) -> Response:
    """Set exactly which of a provider's fetched models may be chosen for a
    chat conversation. Read straight from the raw form rather than a typed
    parameter because the field is a repeated checkbox name, and an
    all-unticked submission sends it zero times."""
    config = await _get_provider_config(db, kind)
    if config is None:
        return await _render_settings(
            request, db, [f'Unknown AI provider "{kind}".'], tab="ai"
        )

    form = await request.form()
    selected = {str(value) for value in form.getlist("enabled_models")}

    result = await db.execute(select(AiModel).where(AiModel.provider_id == config.id))
    for model in result.scalars().all():
        model.enabled = model.model_id in selected
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.ai_models.update",
        summary=f"Enabled {len(selected)} model(s) for chat on AI provider {config.kind.value}",
        details={"provider": config.kind.value, "enabled_models": sorted(selected)},
    )
    return RedirectResponse(url="/settings?tab=ai", status_code=status.HTTP_303_SEE_OTHER)


def _parse_optional_limit(raw: str, label: str, errors: list[str]) -> int | None:
    value = raw.strip()
    if value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        errors.append(f'The {label} limit "{value}" isn\'t a whole number of tokens.')
        return None
    if parsed < 0:
        errors.append(f"The {label} limit must not be negative.")
        return None
    return parsed


@router.post("/ai-limits", dependencies=[_manage, Depends(verify_csrf)])
async def update_ai_limits(
    request: Request,
    db: AsyncSession = Depends(get_db),
    ai_daily_token_limit: str = Form(""),
    ai_weekly_token_limit: str = Form(""),
    ai_monthly_token_limit: str = Form(""),
) -> Response:
    app_settings = await get_or_create_app_settings(db)
    errors: list[str] = []
    daily = _parse_optional_limit(ai_daily_token_limit, "daily", errors)
    weekly = _parse_optional_limit(ai_weekly_token_limit, "weekly", errors)
    monthly = _parse_optional_limit(ai_monthly_token_limit, "monthly", errors)
    if errors:
        return await _render_settings(request, db, errors, tab="ai")

    app_settings.ai_daily_token_limit = daily
    app_settings.ai_weekly_token_limit = weekly
    app_settings.ai_monthly_token_limit = monthly
    await db.commit()

    await log_event(
        db,
        request=request,
        action="settings.ai_limits.update",
        summary="Updated the AI token limits",
        details={"daily": daily, "weekly": weekly, "monthly": monthly},
    )
    return RedirectResponse(url="/settings?tab=ai", status_code=status.HTTP_303_SEE_OTHER)
