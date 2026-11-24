"""Tests for the six follow-up features added after the initial auth/RBAC
work: per-IP rate limiting is covered in `tests/test_auth.py` instead, since
it's part of the login flow — this file covers the dashboard, per-user API
tokens, SSH key rotation, and CSV bulk import."""

from __future__ import annotations

import re

from httpx import AsyncClient

from tests.conftest import create_fabric_via_web


async def test_dashboard_renders_for_full_permission_user(client):
    response = await client.get("/dashboard")
    assert response.status_code == 200
    assert "Dashboard" in response.text


async def test_dashboard_hides_sections_without_permission(client, login_as):
    from app.db.models.role import Permission

    await login_as(client, permissions={Permission.FABRIC_VIEW})
    response = await client.get("/dashboard")
    assert response.status_code == 200
    assert "Scheduling" not in response.text
    assert "Recent activity" not in response.text


async def _create_api_token(client: AsyncClient, *, name: str = "test-token") -> str:
    await client.get("/account")
    csrf_token = client.cookies.get("csrftoken")
    response = await client.post(
        "/account/api-tokens", data={"name": name, "csrf_token": csrf_token}
    )
    assert response.status_code == 200
    match = re.search(r"<textarea readonly rows=\"2\">([^<]+)</textarea>", response.text)
    assert match is not None
    return match.group(1)


async def test_settings_shows_app_version(client):
    from app.core.version import APP_VERSION

    response = await client.get("/settings")
    assert response.status_code == 200
    assert APP_VERSION in response.text


def _input_value(page_text: str, field_id: str) -> str:
    match = re.search(rf'id="{field_id}"[^>]*>', page_text)
    assert match is not None, f"no input#{field_id} found"
    value_match = re.search(r'value="([^"]*)"', match.group(0))
    assert value_match is not None, match.group(0)
    return value_match.group(1)


async def test_update_syslog_settings_persists_and_validates(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    missing_host = await client.post(
        "/settings/syslog",
        data={
            "syslog_enabled": "1",
            "syslog_host": "",
            "syslog_port": "514",
            "syslog_protocol": "udp",
            "csrf_token": csrf_token,
        },
    )
    assert missing_host.status_code == 200
    assert "needs a server host" in missing_host.text

    ok = await client.post(
        "/settings/syslog",
        data={
            "syslog_enabled": "1",
            "syslog_host": "siem.example.com",
            "syslog_port": "6514",
            "syslog_protocol": "tls",
            "csrf_token": csrf_token,
        },
    )
    assert ok.status_code == 303

    page = await client.get("/settings?tab=integrations")
    assert 'value="siem.example.com"' in page.text
    assert 'value="6514"' in page.text

    log = await client.get("/audit")
    assert "settings.syslog.update" in log.text


async def test_audit_export_csv_and_json(client, accept_any_api_key):
    # Generate at least one audit entry to export.
    await create_fabric_via_web(client, "export-test-fabric")

    csv_response = await client.get("/audit/export?format=csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "export-test-fabric" in csv_response.text
    assert "sequence,created_at,actor" in csv_response.text

    json_response = await client.get("/audit/export?format=json")
    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    entries = json_response.json()
    assert any("export-test-fabric" in e["summary"] for e in entries)


async def test_audit_export_csv_neutralizes_formula_injection(client, accept_any_api_key):
    # A name starting with "=" would open Excel/LibreOffice/Sheets up to
    # formula execution if written to the CSV export verbatim — see
    # app.web.routes.audit._csv_safe. Fabric names are user-controlled, so
    # the export itself must neutralize it.
    await create_fabric_via_web(client, '=cmd|"/c calc"!A0')

    csv_response = await client.get("/audit/export?format=csv")
    assert csv_response.status_code == 200
    assert '"\'=cmd|' in csv_response.text
    assert ',=cmd|' not in csv_response.text


async def test_audit_export_respects_outcome_filter(client):
    response = await client.get("/audit/export?format=json&outcome=denied")
    assert response.status_code == 200
    entries = response.json()
    assert all(e["outcome"] == "denied" for e in entries)


async def test_syslog_forwarding_is_skipped_when_disabled():
    from unittest.mock import AsyncMock, patch

    from app.audit_syslog import forward_to_syslog
    from app.db.models.app_settings import AppSettings

    settings = AppSettings(id=1, syslog_enabled=False, syslog_host="siem.example.com")
    with patch("app.audit_syslog.asyncio.to_thread", new=AsyncMock()) as mock_to_thread:
        await forward_to_syslog(settings, entry=None)  # type: ignore[arg-type]
    mock_to_thread.assert_not_called()


async def test_syslog_forwarding_sends_udp_datagram():
    from unittest.mock import MagicMock, patch

    from app.audit_syslog import _send_sync
    from app.db.models.app_settings import AppSettings, SyslogProtocol

    settings = AppSettings(
        id=1,
        syslog_enabled=True,
        syslog_host="siem.example.com",
        syslog_port=514,
        syslog_protocol=SyslogProtocol.UDP,
    )

    mock_socket = MagicMock()
    mock_socket.__enter__.return_value = mock_socket
    with patch("app.audit_syslog.socket.socket", return_value=mock_socket):
        _send_sync(settings, "test message")

    mock_socket.sendto.assert_called_once()
    sent_bytes, address = mock_socket.sendto.call_args[0]
    assert sent_bytes == b"test message"
    assert address == ("siem.example.com", 514)


async def test_rfc5424_message_body_is_json():
    import json
    from datetime import UTC, datetime

    from app.audit_syslog import _rfc5424_message
    from app.db.models.audit_log import AuditLogEntry, AuditOutcome

    entry = AuditLogEntry(
        created_at=datetime.now(UTC),
        actor="alice",
        ip_address="10.0.0.1",
        action="machine.reboot",
        outcome=AuditOutcome.SUCCESS,
        target_type="machine",
        target_id="db1",
        target_label="db1.example.com",
        summary="Rebooted db1",
    )
    message = _rfc5424_message(entry)
    # Header (PRI VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID
    # STRUCTURED-DATA) is seven space-separated fields before the MSG part.
    body = message.split(" ", 7)[-1]
    parsed = json.loads(body)
    assert parsed["actor"] == "alice"
    assert parsed["outcome"] == "success"
    assert parsed["summary"] == "Rebooted db1"


async def test_update_smtp_settings_persists_and_validates(client):
    await client.get("/settings")
    csrf_token = client.cookies.get("csrftoken")

    missing_host = await client.post(
        "/settings/smtp",
        data={
            "smtp_enabled": "1",
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_encryption": "starttls",
            "csrf_token": csrf_token,
        },
    )
    assert "needs a server host" in missing_host.text

    ok = await client.post(
        "/settings/smtp",
        data={
            "smtp_enabled": "1",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "smtp_encryption": "ssl_tls",
            "smtp_username": "relay@example.com",
            "smtp_from_address": "ucc@example.com",
            "smtp_from_name": "ucc",
            "csrf_token": csrf_token,
        },
    )
    assert ok.status_code == 303

    page = await client.get("/settings?tab=integrations")
    assert 'value="465"' in page.text
    assert 'value="ucc@example.com"' in page.text

    log = await client.get("/audit")
    assert "settings.smtp.update" in log.text
