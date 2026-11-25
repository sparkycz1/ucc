"""Record each periodic job's last run for the Status page (`JobRun`).

A Beat task's one-line wrapper becomes
`asyncio.run(tracked("refresh-release-catalog", _refresh_release_catalog()))`
— the job's own coroutine is unchanged, so tests keep calling it directly,
and recording happens only in a real worker. Recording is best-effort: a
failure to write the status row is logged and never breaks the job, and the
job's own exception is recorded and re-raised (Celery still sees it).

The name is the job's `beat_schedule` key (`app.tasks.celery_app`), which
is what the Status page lists.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

from app.db import session as db_session
from app.db.models.job_run import JobRun

logger = logging.getLogger(__name__)

_MAX_TEXT = 1000


def summarize(result: Any) -> str | None:
    """A job's return value as one short line: `{"ok": True, "catalog": 20}`
    → "catalog 20"; an int → "20"; nothing → None."""
    if result is None:
        return None
    if isinstance(result, dict):
        parts = [f"{key} {value}" for key, value in result.items() if key != "ok"]
        if result.get("ok") is False and "error" not in result:
            parts.insert(0, "not ok")
        return ", ".join(parts)[:_MAX_TEXT] or None
    return str(result)[:_MAX_TEXT]


async def _record(name: str, **values: Any) -> None:
    try:
        async with db_session.AsyncSessionLocal() as session:
            row = await session.get(JobRun, name)
            if row is None:
                row = JobRun(name=name)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()
    except Exception:  # noqa: BLE001 — the status board must never break the job
        logger.warning("Could not record the run of %s", name, exc_info=True)


async def tracked[T](name: str, work: Awaitable[T]) -> T:
    await _record(name, last_started_at=datetime.now(UTC))
    try:
        result = await work
    except Exception as exc:
        now = datetime.now(UTC)
        message = f"{exc.__class__.__name__}: {exc}"[:_MAX_TEXT]
        await _record(
            name,
            last_finished_at=now,
            last_ok=False,
            last_result=None,
            last_error_at=now,
            last_error=message,
        )
        raise
    failed = result.get("error") if isinstance(result, dict) and result.get("ok") is False else None
    ok = failed is None and not (isinstance(result, dict) and result.get("ok") is False)
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "last_finished_at": now,
        "last_ok": ok,
        "last_result": summarize(result),
    }
    if not ok:
        values["last_error_at"] = now
        values["last_error"] = str(failed or summarize(result))[:_MAX_TEXT]
    await _record(name, **values)
    return result
