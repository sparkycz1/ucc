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
