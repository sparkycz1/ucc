from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models.scheduled_task import ScheduleTargetType
from app.scheduling.actions import (
    ActionRunResult,
    ScheduledActionParam,
    ScheduledActionSpec,
    all_actions,
    get_action,
    register_action,
)
from app.scheduling.builtin_actions import register_builtin_actions
from app.scheduling.cron import compute_next_run, validate_cron_expression
from app.scheduling.targets import decode_target, encode_target


def test_validate_cron_expression_accepts_standard_5_field():
    validate_cron_expression("0 3 * * *")  # must not raise


def test_validate_cron_expression_rejects_garbage():
    with pytest.raises(ValueError, match="not a valid cron expression"):
        validate_cron_expression("not a cron expression")


def test_compute_next_run_is_strictly_after_base():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    next_run = compute_next_run("0 3 * * *", after=base)

    assert next_run > base
    assert next_run == datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def test_compute_next_run_every_six_hours():
    base = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    next_run = compute_next_run("0 */6 * * *", after=base)

    assert next_run == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_encode_decode_target_round_trips_all_fabrics():
    encoded = encode_target(ScheduleTargetType.ALL_FABRICS, None, None)
    assert encoded == "all"
    assert decode_target(encoded) == (ScheduleTargetType.ALL_FABRICS, None, None)


def test_encode_decode_target_round_trips_fabric():
    fabric_id = uuid.uuid4()
    encoded = encode_target(ScheduleTargetType.FABRIC, fabric_id, None)
    assert decode_target(encoded) == (ScheduleTargetType.FABRIC, fabric_id, None)


def test_encode_decode_target_round_trips_site():
    site_id = uuid.uuid4()
    # A site target also stores its Fabric, but the encoding only needs the
    # site — the route fills the Fabric in from it.
    encoded = encode_target(ScheduleTargetType.SITE, uuid.uuid4(), site_id)
    assert decode_target(encoded) == (ScheduleTargetType.SITE, None, site_id)


def test_decode_target_rejects_garbage():
    with pytest.raises(ValueError, match="Invalid target"):
        decode_target("bogus")


def test_register_builtin_actions_is_idempotent():
    register_builtin_actions()
    register_builtin_actions()
    keys = [action.key for action in all_actions()]
    assert len(keys) == len(set(keys))
    assert "sync_inventory" in keys


def test_sync_inventory_action_is_not_destructive_and_needs_no_extra_permission():
    register_builtin_actions()
    action = get_action("sync_inventory")
    assert action is not None
    assert action.destructive is False
    assert action.extra_permission is None


async def test_sync_inventory_enqueues_one_sync_per_fabric(celery_calls):
    from app.db.models.fabric import Fabric
    from app.scheduling.targets import ResolvedTarget

    register_builtin_actions()
    action = get_action("sync_inventory")
    assert action is not None
    fabrics = [Fabric(id=uuid.uuid4(), name=n, api_key_encrypted=b"k") for n in ("a", "b")]
    result = await action.run(None, ResolvedTarget(fabrics=fabrics), {})  # type: ignore[arg-type]

    assert result == ActionRunResult(attempted=2, skipped=0)
    assert celery_calls.names == ["app.tasks.jobs.sync_fabric", "app.tasks.jobs.sync_fabric"]


def test_scheduled_action_param_defaults_to_select_with_no_choices():
    param = ScheduledActionParam(key="x", label="X", default="")
    assert param.param_type == "select"
    assert param.choices == []


def test_register_action_rejects_duplicate_key():
    async def _run(db, target, params):  # pragma: no cover - never invoked
        return ActionRunResult(attempted=0, skipped=0)

    spec = ScheduledActionSpec(key="a-test-only-action", label="x", description="x", run=_run)
    register_action(spec)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_action(spec)
    finally:
        # Don't leak this test-only action into other tests' registry state.
        from app.scheduling import actions as actions_module

        actions_module._REGISTRY.pop("a-test-only-action", None)
