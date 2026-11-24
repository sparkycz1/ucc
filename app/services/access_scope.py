"""The single implementation of per-user Fabric visibility scoping.

See `app.db.models.user_fabric_access` for the data model and the "empty
means unrestricted" rule this enforces. Everything that lists or resolves a
`Fabric` — or anything synced under one (console, site, device) — for a
*specific user* goes through this module (web routes and the AI assistant's
tools alike), so there is exactly one definition of "may this account see
this".

Detail routes that find an out-of-scope object answer 404, never 403: an
error naming an id the caller can't see would confirm it exists.

What is deliberately **not** scoped: the audit log (`audit.view` stays a
single global permission — the audit trail is a security control over the
whole deployment), and background jobs (a Celery task has no "current
user"). Scope on scheduled tasks is enforced when one is *created or
edited*, by whoever is doing that — never at execution time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.fabric import Fabric
from app.db.models.user import User
from app.db.models.user_fabric_access import UserFabricAccess


async def allowed_fabric_ids(db: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """The Fabrics `user` is restricted to, or `None` when they are not
    restricted at all. `None` and an empty set mean opposite things — a user
    with no rows is unrestricted by definition, so `set()` is never
    returned from here."""
    result = await db.execute(
        select(UserFabricAccess.fabric_id).where(UserFabricAccess.user_id == user.id)
    )
    return set(result.scalars().all()) or None


async def is_restricted(db: AsyncSession, user: User) -> bool:
    return await allowed_fabric_ids(db, user) is not None


async def fabrics_visible_to(db: AsyncSession, user: User) -> Select[Fabric]:
    """A `Select` for `Fabric`, already scope-filtered and still composable."""
    query = select(Fabric)
    fabric_ids = await allowed_fabric_ids(db, user)
    if fabric_ids is not None:
        query = query.where(Fabric.id.in_(fabric_ids))
    return query


async def count_visible_fabrics(db: AsyncSession, user: User) -> int:
    """Counted via `maintain_column_froms=True`: without it, the unrestricted
    case (no WHERE) silently becomes a bare `SELECT count(*)` that answers 1."""
    query = await fabrics_visible_to(db, user)
    return (
        await db.execute(query.with_only_columns(func.count(), maintain_column_froms=True))
    ).scalar_one()


async def can_see_fabric_id(db: AsyncSession, user: User, fabric_id: uuid.UUID | None) -> bool:
    fabric_ids = await allowed_fabric_ids(db, user)
    if fabric_ids is None:
        return True
    return fabric_id is not None and fabric_id in fabric_ids


async def set_fabric_access(
    db: AsyncSession, user_id: uuid.UUID, fabric_ids: Iterable[uuid.UUID]
) -> None:
    """Replace a user's entire grant set (delete-then-insert). Does not
    commit — the caller's transaction does."""
    await db.execute(delete(UserFabricAccess).where(UserFabricAccess.user_id == user_id))
    for fabric_id in dict.fromkeys(fabric_ids):
        db.add(UserFabricAccess(user_id=user_id, fabric_id=fabric_id))


async def fabric_names_for(db: AsyncSession, fabric_ids: Iterable[uuid.UUID]) -> list[str]:
    """Names behind a set of Fabric ids, sorted — for audit summaries."""
    ids = list(fabric_ids)
    if not ids:
        return []
    result = await db.execute(select(Fabric.name).where(Fabric.id.in_(ids)).order_by(Fabric.name))
    return list(result.scalars().all())
