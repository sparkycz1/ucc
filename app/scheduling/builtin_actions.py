"""Registers the schedulable actions that exist today.

`register_builtin_actions()` is called from `app.main` at import time (so the
web UI's "New scheduled task" form has something to list), from
`app.scheduling.jobs` at import time, and again from each forked Celery
worker child (`app.tasks.celery_app`'s `worker_process_init` handler) — it's
idempotent, so calling it from all three is harmless.

To make a new feature schedulable: write an `ActionRunFunc` and add one
`register_action(ScheduledActionSpec(...))` call below. The schedule form,
validation, and the scheduler tick all read from the registry.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.permission import Permission
from app.scheduling.actions import (
    ActionRunResult,
    ScheduledActionSpec,
    get_action,
    register_action,
)
from app.scheduling.targets import ResolvedTarget


async def _run_sync_inventory(
    db: AsyncSession, target: ResolvedTarget, params: dict[str, str]
) -> ActionRunResult:
    """Syncs whole Fabrics — a site target syncs the Fabric it belongs to,
    since Site Manager has no per-site inventory endpoint."""
    from app.tasks.jobs import sync_fabric

    for fabric in target.fabrics:
        sync_fabric.delay(str(fabric.id))
    return ActionRunResult(attempted=len(target.fabrics), skipped=0)


async def _run_assess_releases(
    db: AsyncSession, target: ResolvedTarget, params: dict[str, str]
) -> ActionRunResult:
    """Re-rates every release the target's consoles/devices have a pending
    update to — one Celery task per release, never awaited here. Releases
    with a manual level or a manual allowance are skipped (a human already
    decided), as is everything when no model is configured."""
    from app.core.app_settings import get_or_create_app_settings
    from app.releases.service import assessment_model, ensure_releases
    from app.releases.verdicts import installed_keys, load_latest, load_releases, update_targets
    from app.tasks.release_jobs import assess_release, load_consoles

    app_settings = await get_or_create_app_settings(db)
    if await assessment_model(db, app_settings) is None:
        return ActionRunResult(attempted=0, skipped=len(target.fabrics))
    wanted: dict[str, bool] = {}
    for fabric in target.fabrics:
        consoles = await load_consoles(db, fabric.id)
        targets = update_targets(consoles, await load_latest(db, installed_keys(consoles)))
        if target.sites is not None:
            # A site target: only that site's devices (console-level OS and
            # applications serve every site on the console, so they stay).
            site_ids = {site.id for site in target.sites}
            targets = [t for t in targets if t.device is None or t.device.site_id in site_ids]
        await ensure_releases(db, targets)
        for release in (await load_releases(db, targets)).values():
            human_decided = release.manual_risk_level is not None or release.is_allowed
            wanted[str(release.id)] = not human_decided
    await db.commit()
    for release_id, run in wanted.items():
        if run:
            assess_release.delay(release_id)
    attempted = sum(wanted.values())
    return ActionRunResult(attempted=attempted, skipped=len(wanted) - attempted)


def register_builtin_actions() -> None:
    if get_action("sync_inventory") is not None:
        return  # already registered — safe to call from multiple entry points

    register_action(
        ScheduledActionSpec(
            key="sync_inventory",
            label="Sync inventory",
            description=(
                "Re-reads consoles, applications, sites, devices and their "
                "versions from Site Manager right away, instead of waiting for "
                "the periodic sync."
            ),
            run=_run_sync_inventory,
        )
    )
    register_action(
        ScheduledActionSpec(
            key="assess_releases",
            label="Re-assess releases",
            description=(
                "Asks the AI to rate again every release the target's consoles "
                "and devices could update to, with what the community has said "
                "since. Needs a model in Settings → Releases. Skips releases a "
                "person has rated or allowed by hand."
            ),
            run=_run_assess_releases,
            extra_permission=Permission.RELEASE_MANAGE,
        )
    )
