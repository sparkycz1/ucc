"""Registry of actions that can be attached to a scheduled task.

This is the extension point: adding a new schedulable action for some new
feature means writing one `run` function and calling `register_action()`
once (see `app.scheduling.builtin_actions`) —
nothing about the schedule model, the scheduler tick, or the "New scheduled
task" form needs to change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.db.models.role import Permission
    from app.scheduling.targets import ResolvedTarget


@dataclass(frozen=True)
class ActionRunResult:
    """What happened when a scheduled task fired, for `last_run_summary`."""

    attempted: int
    skipped: int


@dataclass(frozen=True)
class ScheduledActionParam:
    """One configurable option for an action, rendered in the scheduling
    form as either a `<select>` (`param_type="select"`) or a free-text
    `<input>` (`param_type="text"`).
    `choices` is only meaningful (and required) for `"select"`."""

    key: str
    label: str
    default: str
    param_type: Literal["select", "text"] = "select"
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, display label)
    placeholder: str = ""


# (db session, resolved target, the action's stored params) -> what happened.
# No queue handle is threaded through: an action enqueues Celery tasks by
# importing and calling them.
ActionRunFunc = Callable[
    [AsyncSession, "ResolvedTarget", dict[str, str]], Awaitable[ActionRunResult]
]


@dataclass(frozen=True)
class ScheduledActionSpec:
    key: str
    label: str
    description: str
    run: ActionRunFunc
    params: list[ScheduledActionParam] = field(default_factory=list)
    # Shown as a caution in the schedule form for actions with no undo.
    destructive: bool = False
    # An action a user needs more than plain `scheduling.manage` to create
    # or edit a task for — e.g. a scheduled update also requires
    # `update.run`, the same gate running one by hand needs. Checked in
    # `app.web.routes.scheduling`, not enforced here.
    extra_permission: Permission | None = None


_REGISTRY: dict[str, ScheduledActionSpec] = {}


def register_action(spec: ScheduledActionSpec) -> None:
    if spec.key in _REGISTRY:
        raise ValueError(f'A schedulable action "{spec.key}" is already registered.')
    _REGISTRY[spec.key] = spec


def get_action(key: str) -> ScheduledActionSpec | None:
    return _REGISTRY.get(key)


def all_actions() -> list[ScheduledActionSpec]:
    return list(_REGISTRY.values())
