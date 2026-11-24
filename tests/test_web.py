from __future__ import annotations

import re

from app.main import app


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_root_redirects_to_dashboard(client):
    response = await client.get("/")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/dashboard"


async def test_users_page_lists_the_logged_in_admin(client):
    from tests.conftest import ADMIN_USERNAME

    users = await client.get("/users")
    assert users.status_code == 200
    assert ADMIN_USERNAME in users.text


async def test_settings_audit_retention_defaults_to_forever(client):
    response = await client.get("/settings?tab=security")
    assert response.status_code == 200
    assert "forever" in response.text


async def test_update_audit_retention_persists_value(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/settings/audit-retention", data={"retention_days": "90", "csrf_token": csrf_token}
    )
    assert response.status_code == 303

    page = await client.get("/settings?tab=security")
    assert 'value="90"' in page.text

    log = await client.get("/audit")
    assert "settings.audit_retention.update" in log.text
    assert "90 day" in log.text


async def test_update_audit_retention_empty_means_forever(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    await client.post(
        "/settings/audit-retention", data={"retention_days": "30", "csrf_token": csrf_token}
    )
    response = await client.post(
        "/settings/audit-retention", data={"retention_days": "", "csrf_token": csrf_token}
    )
    assert response.status_code == 303

    page = await client.get("/settings?tab=security")
    assert "keep forever" in page.text.lower() or "forever" in page.text


async def test_update_audit_retention_rejects_non_integer(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/settings/audit-retention", data={"retention_days": "banana", "csrf_token": csrf_token}
    )
    assert response.status_code == 200
    assert "isn&#39;t a whole number" in response.text or "whole number" in response.text


async def test_update_audit_retention_rejects_negative(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/settings/audit-retention", data={"retention_days": "-5", "csrf_token": csrf_token}
    )
    assert response.status_code == 200
    assert "whole number" in response.text


async def test_verify_audit_chain_endpoint_reports_intact_chain(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")
    # Generate at least one chained entry first.
    await client.post(
        "/settings/audit-retention", data={"retention_days": "30", "csrf_token": csrf_token}
    )

    response = await client.post("/settings/audit-verify", data={"csrf_token": csrf_token})
    assert response.status_code == 200
    assert "verified intact" in response.text

    log = await client.get("/audit")
    assert "audit_log.verify" in log.text


def _extract_scheduled_task_id(listing_html: str) -> str:
    # The page header's "New scheduled task" link (/scheduling/new) also
    # starts with "/scheduling/", so a plain string split isn't safe here —
    # match the actual per-row edit link instead.
    match = re.search(r"/scheduling/([0-9a-f-]{36})/edit", listing_html)
    assert match is not None
    return match.group(1)


async def test_scheduling_nav_link_present(client):
    response = await client.get("/dashboard")
    assert 'href="/scheduling"' in response.text


async def test_scheduling_empty_state(client):
    response = await client.get("/scheduling")
    assert response.status_code == 200
    assert "No scheduled tasks yet" in response.text


async def test_new_scheduled_task_form_lists_registered_actions(client):
    response = await client.get("/scheduling/new")
    assert response.status_code == 200
    assert "Sync inventory" in response.text


async def test_create_scheduled_task_requires_csrf_token(client):
    response = await client.post(
        "/scheduling",
        data={
            "name": "nightly",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "0 3 * * *",
            "csrf_token": "wrong",
        },
    )
    assert response.status_code == 403


async def test_create_scheduled_task_rejects_invalid_cron(client):
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/scheduling",
        data={
            "name": "nightly",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "not a cron",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 422
    assert "not a valid cron expression" in response.text


async def test_create_and_list_scheduled_task_for_all_fabrics(client):
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    create = await client.post(
        "/scheduling",
        data={
            "name": "nightly check",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "0 3 * * *",
            "is_enabled": "on",
            "csrf_token": csrf_token,
        },
    )
    assert create.status_code == 303
    assert create.headers["location"] == "/scheduling"

    listing = await client.get("/scheduling")
    assert "nightly check" in listing.text
    assert "All Fabrics" in listing.text
    assert "Sync inventory" in listing.text  # human label, not the raw "sync_inventory" key
    assert "0 3 * * *" in listing.text
    assert "badge-ok" in listing.text  # enabled


async def test_new_scheduled_task_form_defaults_to_enabled(client):
    response = await client.get("/scheduling/new")
    assert 'name="is_enabled" checked' in response.text


async def test_failed_scheduled_task_submission_preserves_unchecked_enabled_box(client):
    # Regression check: a failed-validation re-render must reflect exactly
    # what was submitted, not silently reapply the "new form" default of
    # enabled — otherwise unchecking "Enabled" then hitting another error
    # would look like it was ignored.
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/scheduling",
        data={
            "name": "disabled-on-create",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "not a cron",
            # "is_enabled" deliberately omitted — an unchecked checkbox.
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 422
    assert 'name="is_enabled" checked' not in response.text


async def test_scheduled_task_target_must_be_a_known_fabric_or_site(client):
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    response = await client.post(
        "/scheduling",
        data={
            "name": "bogus target",
            "target": "bogus",
            "action": "sync_inventory",
            "cron_expression": "0 3 * * *",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 422
    assert "Invalid target" in response.text


async def test_toggle_and_run_now_and_delete_scheduled_task(client):
    await client.get("/scheduling/new")
    csrf_token = client.cookies.get("csrftoken")

    create = await client.post(
        "/scheduling",
        data={
            "name": "toggle-me",
            "target": "all",
            "action": "sync_inventory",
            "cron_expression": "0 3 * * *",
            "is_enabled": "on",
            "csrf_token": csrf_token,
        },
    )
    assert create.status_code == 303

    listing = await client.get("/scheduling")
    task_id = _extract_scheduled_task_id(listing.text)

    # Disable — next_run_at is cleared, badge flips to "disabled".
    toggled = await client.post(f"/scheduling/{task_id}/toggle", data={"csrf_token": csrf_token})
    assert toggled.status_code == 303
    disabled_listing = await client.get("/scheduling")
    assert "badge-warn" in disabled_listing.text

    # Re-enable.
    await client.post(f"/scheduling/{task_id}/toggle", data={"csrf_token": csrf_token})

    # Run now — enqueues the same job the per-minute tick would.
    run_now = await client.post(f"/scheduling/{task_id}/run-now", data={"csrf_token": csrf_token})
    assert run_now.status_code == 303
    assert "app.scheduling.jobs.run_scheduled_task" in app.state.celery_calls.names
    assert (task_id,) == app.state.celery_calls[-1][1]

    ran_listing = await client.get(run_now.headers["location"])
    assert "Run enqueued" in ran_listing.text

    # Delete.
    deleted = await client.post(f"/scheduling/{task_id}/delete", data={"csrf_token": csrf_token})
    assert deleted.status_code == 303
    final_listing = await client.get("/scheduling")
    assert "toggle-me" not in final_listing.text
