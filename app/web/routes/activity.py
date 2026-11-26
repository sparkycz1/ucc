"""The activity indicator in the page header: what background work runs
right now — inventory syncs, the catalogue refresh, assessments calling
the AI and the ones waiting for a slot.

An HTML fragment (no JSON endpoint — CLAUDE.md), polled by
`static/js/activity.js` every few seconds while something runs and every
15 s otherwise; when the work is done that script shows a notice and
re-fetches the page's own content. Only counts — nothing scoped to a
Fabric is named, so any signed-in account may see it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import slots as ai_slots
from app.core.app_settings import get_or_create_app_settings
from app.db.session import get_db
from app.releases.service import assessment_queue
from app.services import activity
from app.web.templating import templates

router = APIRouter()


@router.get("/activity")
async def activity_fragment(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    queue = await assessment_queue(db, await get_or_create_app_settings(db))
    assessing = await ai_slots.in_use() or 0
    counts = {
        "syncs": await activity.count("sync"),
        "catalog": await activity.count("catalog"),
        "assessing": assessing,
        # Queued includes the ones running; show those waiting apart.
        "waiting": max(queue.queued - assessing, 0),
    }
    response = templates.TemplateResponse(
        request,
        "partials/_activity.html",
        {"counts": counts, "busy": sum(counts.values())},
    )
    response.headers["Cache-Control"] = "no-store"
    return response
