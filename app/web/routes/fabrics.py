"""Fabrics — add a UniFi Site Manager API key, and browse everything synced
under it (consoles and their UniFi OS / application versions, sites,
devices and their firmware).

`fabric.view` lists and opens Fabrics (scoped per user — see
`app.services.access_scope`; an out-of-scope Fabric is a 404) and can ask
for an immediate re-sync. `fabric.manage` adds, edits and deletes them —
that is where an API key is entered, so it is its own trust level.

The API key is validated against Site Manager on create and on replace,
stored only via `encrypt_secret`, and never rendered back into a page.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit import log_event
from app.auth.dependencies import get_current_user, require_permission
from app.core.app_settings import get_or_create_app_settings
from app.core.csrf import get_or_create_csrf_token, set_csrf_cookie, verify_csrf
from app.core.security import encrypt_secret
from app.db.models.audit_log import AuditOutcome
from app.db.models.console import Console
from app.db.models.device import Device
from app.db.models.fabric import Fabric
from app.db.models.release import ReleaseKind
from app.db.models.role import Permission
from app.db.models.user import User
from app.db.session import get_db
from app.releases.verdicts import (
    TargetVerdict,
    Verdict,
    auto_update_applies,
    installed_keys,
    installed_targets,
    load_latest,
    load_releases,
    max_risk_for,
    update_targets,
    verdicts,
)
from app.schemas.fabric import FabricForm
from app.services.access_scope import can_see_fabric_id, fabrics_visible_to
from app.tasks.jobs import sync_fabric
from app.unifi.client import SiteManagerClient, SiteManagerError
from app.web.templating import templates

router = APIRouter(
    prefix="/fabrics", dependencies=[Depends(require_permission(Permission.FABRIC_VIEW))]
)
_manage = Depends(require_permission(Permission.FABRIC_MANAGE))


async def _get_fabric_or_404(db: AsyncSession, user: User, fabric_id: uuid.UUID) -> Fabric:
    fabric = await db.get(Fabric, fabric_id)
    if fabric is None or not await can_see_fabric_id(db, user, fabric.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fabric not found.")
    return fabric


async def _validate_api_key(api_key: str) -> str | None:
    """`None` if Site Manager accepts the key, else a human-readable error.
    Lists hosts once — the cheapest call that proves the key works."""
    try:
        async with SiteManagerClient(api_key) as client:
            await client.hosts()
    except SiteManagerError as exc:
        if exc.status in (401, 403):
            return "UniFi Site Manager rejected this API key."
        return f"Could not reach UniFi Site Manager: {exc}"
    return None


def _form_from_request(raw: dict[str, str]) -> dict[str, object]:
    return {
        "name": raw.get("name", ""),
        "description": raw.get("description", "").strip() or None,
        "api_key": raw.get("api_key", ""),
        "is_enabled": bool(raw.get("is_enabled")),
        "max_acceptable_risk_level": (raw.get("max_acceptable_risk_level") or "").strip() or None,
    }


def _render_form(
    request: Request,
    template: str,
    *,
    form: dict[str, object],
    errors: list[str],
    fabric: Fabric | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    csrf_token, new_cookie = get_or_create_csrf_token(request)
    # The API key is never echoed back, even after a failed submit.
    safe_form = {k: v for k, v in form.items() if k != "api_key"}
    response = templates.TemplateResponse(
        request,
        template,
        {"form": safe_form, "errors": errors, "fabric": fabric, "csrf_token": csrf_token},
        status_code=status_code,
    )
    if new_cookie:
        set_csrf_cookie(response, new_cookie)
    return response


@router.get("")
async def list_fabrics(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    query = await fabrics_visible_to(db, user)
    fabrics = list(
        (
            await db.execute(
                query.options(selectinload(Fabric.consoles).selectinload(Console.devices)).order_by(
                    Fabric.name
                )
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "fabrics/list.html",
        {"fabrics": fabrics, "csrf_token": request.state.csrf_token},
    )


@router.get("/new", dependencies=[_manage])
async def new_fabric_form(request: Request) -> Response:
    return _render_form(
        request,
        "fabrics/new.html",
        form={"is_enabled": True, "max_acceptable_risk_level": None},
        errors=[],
    )


@router.post("", dependencies=[_manage, Depends(verify_csrf)])
async def create_fabric(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    raw = {key: str(value) for key, value in (await request.form()).items()}
    form = _form_from_request(raw)
    try:
        payload = FabricForm.model_validate(form)
    except ValidationError as exc:
        errors = [str(e["msg"]) for e in exc.errors()]
        return _render_form(
            request,
            "fabrics/new.html",
            form=form,
            errors=errors,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not payload.api_key:
        return _render_form(
            request,
            "fabrics/new.html",
            form=form,
            errors=["An API key is required."],
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    key_error = await _validate_api_key(payload.api_key)
    if key_error:
        await log_event(
            db,
            request=request,
            action="fabric.create",
            summary=f'Rejected new Fabric "{payload.name}": {key_error}',
            outcome=AuditOutcome.FAILURE,
        )
        return _render_form(
            request,
            "fabrics/new.html",
            form=form,
            errors=[key_error],
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    fabric = Fabric(
        name=payload.name,
        description=payload.description,
        api_key_encrypted=encrypt_secret(payload.api_key),
        is_enabled=payload.is_enabled,
        max_acceptable_risk_level=payload.max_acceptable_risk_level,
    )
    db.add(fabric)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await log_event(
            db,
            request=request,
            action="fabric.create",
            summary=f'Rejected new Fabric "{payload.name}": the name already exists',
            outcome=AuditOutcome.FAILURE,
        )
        return _render_form(
            request,
            "fabrics/new.html",
            form=form,
            errors=[f'A Fabric named "{payload.name}" already exists.'],
            status_code=status.HTTP_409_CONFLICT,
        )
    await db.refresh(fabric)
    await log_event(
        db,
        request=request,
        action="fabric.create",
        summary=f'Added Fabric "{fabric.name}"',
        target_type="fabric",
        target_id=fabric.id,
        target_label=fabric.name,
    )
    if fabric.is_enabled:
        sync_fabric.delay(str(fabric.id))
    return RedirectResponse(url=f"/fabrics/{fabric.id}", status_code=status.HTTP_303_SEE_OTHER)


async def _inventory_context(db: AsyncSession, fabric: Fabric) -> dict[str, object]:
    """The inventory plus the GO/STOP verdict of every pending update in it,
    keyed so the template can look one up per row: `("os", console_id)`,
    `("app", console_id, app_name)`, `("device", device_id)`."""
    consoles = await _load_inventory(db, fabric)
    app_settings = await get_or_create_app_settings(db)
    max_level = max_risk_for(fabric, app_settings.max_acceptable_risk_level)
    latest = await load_latest(db, installed_keys(consoles))
    targets = update_targets(consoles, latest)
    running = installed_targets(consoles)
    releases = await load_releases(db, [*targets, *running])
    by_key: dict[tuple[object, ...], TargetVerdict] = {}
    # UniFi's own auto-update would install these STOP updates anyway.
    auto_update_risks: dict[object, list[TargetVerdict]] = {}
    for verdict in verdicts(targets, releases, max_level):
        by_key[_row_key(verdict)] = verdict
        if verdict.verdict == Verdict.STOP and auto_update_applies(verdict.target):
            auto_update_risks.setdefault(verdict.target.console.id, []).append(verdict)
    installed_by_key = {_row_key(v): v for v in verdicts(running, releases, max_level)}
    return {
        "fabric": fabric,
        "consoles": consoles,
        "verdicts": by_key,
        "installed": installed_by_key,
        "auto_update_risks": auto_update_risks,
        "max_risk_level": max_level,
        "fabric_overrides_risk": fabric.max_acceptable_risk_level is not None,
    }


def _row_key(verdict: TargetVerdict) -> tuple[object, ...]:
    target = verdict.target
    if target.device is not None:
        return ("device", target.device.id)
    if target.kind == ReleaseKind.OS:
        return ("os", target.console.id)
    return ("app", target.console.id, target.app_name)


async def _load_inventory(db: AsyncSession, fabric: Fabric) -> list[Console]:
    result = await db.execute(
        select(Console)
        .where(Console.fabric_id == fabric.id)
        .options(
            selectinload(Console.apps),
            selectinload(Console.sites),
            selectinload(Console.devices).selectinload(Device.site),
        )
        .order_by(Console.name)
    )
    consoles = list(result.scalars().all())
    for console in consoles:
        console.devices.sort(key=lambda d: ((d.site.name or "") if d.site else "", d.name or d.mac))
    return consoles


@router.get("/{fabric_id}")
async def show_fabric(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    return templates.TemplateResponse(
        request,
        "fabrics/detail.html",
        {
            **await _inventory_context(db, fabric),
            "csrf_token": request.state.csrf_token,
            "sync_queued": request.query_params.get("sync") == "queued",
        },
    )


@router.get("/{fabric_id}/inventory-panel")
async def fabric_inventory_panel(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """The htmx-refreshed inventory panel — re-fetched on the
    `live-inventory` push after a sync and on a slow poll as fallback."""
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    return templates.TemplateResponse(
        request,
        "fabrics/_inventory.html",
        await _inventory_context(db, fabric),
    )


@router.post("/{fabric_id}/sync", dependencies=[Depends(verify_csrf)])
async def trigger_sync(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Read-only against UniFi (it only re-reads inventory), so plain
    `fabric.view` may ask for it."""
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    sync_fabric.delay(str(fabric.id))
    await log_event(
        db,
        request=request,
        action="fabric.sync.request",
        summary=f'Requested an inventory sync of Fabric "{fabric.name}"',
        target_type="fabric",
        target_id=fabric.id,
        target_label=fabric.name,
    )
    return RedirectResponse(
        url=f"/fabrics/{fabric.id}?sync=queued", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{fabric_id}/edit", dependencies=[_manage])
async def edit_fabric_form(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    return _render_form(
        request,
        "fabrics/edit.html",
        form={
            "name": fabric.name,
            "description": fabric.description,
            "is_enabled": fabric.is_enabled,
            "max_acceptable_risk_level": fabric.max_acceptable_risk_level,
        },
        errors=[],
        fabric=fabric,
    )


@router.post("/{fabric_id}/edit", dependencies=[_manage, Depends(verify_csrf)])
async def update_fabric(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    raw = {key: str(value) for key, value in (await request.form()).items()}
    form = _form_from_request(raw)
    try:
        payload = FabricForm.model_validate(form)
    except ValidationError as exc:
        return _render_form(
            request,
            "fabrics/edit.html",
            form=form,
            errors=[str(e["msg"]) for e in exc.errors()],
            fabric=fabric,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    key_replaced = False
    if payload.api_key:
        key_error = await _validate_api_key(payload.api_key)
        if key_error:
            await log_event(
                db,
                request=request,
                action="fabric.update",
                summary=f'Rejected API key replacement for Fabric "{fabric.name}": {key_error}',
                outcome=AuditOutcome.FAILURE,
                target_type="fabric",
                target_id=fabric.id,
                target_label=fabric.name,
            )
            return _render_form(
                request,
                "fabrics/edit.html",
                form=form,
                errors=[key_error],
                fabric=fabric,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        fabric.api_key_encrypted = encrypt_secret(payload.api_key)
        fabric.last_sync_error = None
        key_replaced = True

    fabric.name = payload.name
    fabric.description = payload.description
    fabric.is_enabled = payload.is_enabled
    fabric.max_acceptable_risk_level = payload.max_acceptable_risk_level
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _render_form(
            request,
            "fabrics/edit.html",
            form=form,
            errors=[f'A Fabric named "{payload.name}" already exists.'],
            fabric=fabric,
            status_code=status.HTTP_409_CONFLICT,
        )
    await log_event(
        db,
        request=request,
        action="fabric.update",
        summary=(
            f'Updated Fabric "{fabric.name}"'
            + (" and replaced its API key" if key_replaced else "")
        ),
        target_type="fabric",
        target_id=fabric.id,
        target_label=fabric.name,
        details={
            "api_key_replaced": key_replaced,
            "is_enabled": fabric.is_enabled,
            "max_acceptable_risk_level": fabric.max_acceptable_risk_level,
        },
    )
    if key_replaced and fabric.is_enabled:
        sync_fabric.delay(str(fabric.id))
    return RedirectResponse(url=f"/fabrics/{fabric.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{fabric_id}/delete", dependencies=[_manage, Depends(verify_csrf)])
async def delete_fabric(
    request: Request,
    fabric_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    fabric = await _get_fabric_or_404(db, user, fabric_id)
    name = fabric.name
    await db.delete(fabric)
    await db.commit()
    await log_event(
        db,
        request=request,
        action="fabric.delete",
        summary=f'Deleted Fabric "{name}" and its synced inventory',
        target_type="fabric",
        target_id=fabric_id,
        target_label=name,
    )
    return RedirectResponse(url="/fabrics", status_code=status.HTTP_303_SEE_OTHER)
