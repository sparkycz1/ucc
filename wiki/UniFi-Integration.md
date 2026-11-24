# 📡 UniFi Integration

How ucc talks to UniFi, what it stores, and where the AI-gated update
flow is heading.

## Fabrics

A **Fabric** is one UniFi Site Manager API key (create one at
unifi.ui.com → API) and everything that key can see. It is ucc's unit of
ownership:

- per-account scope is granted per Fabric (`app.services.access_scope`,
  `app.db.models.user_fabric_access`) — an account with no Fabric rows
  sees every Fabric, one with rows sees only those, and an out-of-scope
  Fabric is a 404, never a 403;
- a scheduled task targets a site, a Fabric, or all Fabrics
  (`app.scheduling.targets`);
- a notification rule can be limited to Fabrics.

The key is validated against Site Manager (`GET /v1/hosts`) when a Fabric is
added and whenever it is replaced, stored only via `encrypt_secret`
(`Fabric.api_key_encrypted`), never rendered back into a page and never
written to the audit log. Leaving the key field blank on edit keeps the
stored one.

| Permission | Allows |
|---|---|
| `fabric.view` | List and open Fabrics (within the account's Fabric scope), request an immediate sync |
| `fabric.manage` | Add, edit (incl. replacing the key), disable, delete Fabrics |
| `update.run` | Run an update, manually or via a scheduled task *(reserved — next phase)* |
| `update.override` | Allow an update the AI-assessment policy blocked *(reserved — next phase)* |
| `release.manage` | Re-run an AI assessment, set a manual score, edit the acceptable level *(reserved — next phase)* |

## Inventory sync

`app.tasks.jobs.sync_fabric` mirrors one Fabric from Site Manager:

| Site Manager endpoint | Stored as |
|---|---|
| `GET /v1/hosts` | `Console` (UniFi OS version + update available) and its `ConsoleApp` rows (Network, Protect, … with versions) |
| `GET /v1/sites` | `Site` (Site Manager id, legacy short name such as `default`, display name) |
| `GET /v1/devices` | `Device` (MAC, model, firmware version, update available, online) |

Every sync is a full replace: rows the API returns are upserted, rows it
no longer returns are deleted (nothing under a Fabric is ever edited by
hand). `/v1/devices` groups devices by console, not site, so a device's
site is filled in only when the console has exactly one site.

Celery Beat runs `sync_all_fabrics` every `AppSettings.
inventory_sync_interval_seconds` (default 900 s, Settings → Checks &
retention, restart `beat` to apply); it enqueues one `sync_fabric` per
enabled Fabric. The Fabric page's **Sync now** button and the `sync_inventory`
scheduled action do the same on demand. A finished sync rings the live-update
doorbell (`ucc:live:fabric:<id>`), and the Fabric page re-fetches its
inventory panel.

Failures (revoked key, network error) are stored on the Fabric
(`last_sync_error`) and shown on the list and detail pages. Only the
transition from healthy to failing is audit-logged (`fabric.sync`), so a
revoked key doesn't write one audit entry per sweep forever.

Parsing is deliberately tolerant (`app/unifi/client.py`): Ubiquiti's own
spec says payload shapes vary by UniFi Network version, so unknown fields
are ignored and missing ones become `NULL`.

## The connector proxy — and the open question

Site Manager API keys are documented as **read-only**. Updates therefore
can't go through the documented API. The candidate path is the **cloud
connector proxy**, `https://api.ui.com/v1/connector/consoles/{hostId}/…`,
which forwards a request to the console's own local API (console firmware
≥ 5.0.3, 100 requests/min per console, 25 s per request, 10 MB response
limit). What it allows is not documented:

- reading the local Network Integration API — documented by example;
- device firmware upgrades through the legacy `cmd/devmgr` endpoint —
  **unknown**;
- UniFi OS and application (Network/Protect) updates — **unknown**, and
  the proxy may only forward `/proxy/…` paths.

`scripts/probe_connector.py` answers this against a real key. It is
stdlib-only and read-only by default; the only write it can do is one
device upgrade, and only with both `--upgrade-device <mac>` and
`--yes-really-upgrade`. Full responses go to `probe_output/`. That output
contains inventory — don't commit or share it.

```bash
UNIFI_API_KEY=... python3 scripts/probe_connector.py
UNIFI_API_KEY=... python3 scripts/probe_connector.py --host <hostId>
UNIFI_API_KEY=... python3 scripts/probe_connector.py --host <hostId> \
    --upgrade-device <mac> --site default --yes-really-upgrade
```

`SiteManagerClient.connector()` is the client-side hook for whatever the
probe shows works.

## Roadmap: AI-gated updates

Not built yet. The design agreed so far:

1. **Release assessment.** For each version seen in the inventory (UniFi
   OS per console model, each application, each device model's firmware),
   an AI job reads the official community.ui.com release thread and its
   comments plus r/Ubiquiti and r/UNIFI, and rates the version **1–5**
   (1 = stable … 5 = broken) with a written reason and links to the posts
   it relied on. Versions are re-assessed over time, because complaints
   arrive after the release. Uses the AI providers already configured on
   Settings → AI; the `release.assessed` notification event already exists.
2. **Acceptable level.** One global maximum risk level, overridable per
   Fabric, per site and per device type.
3. **Updates.** A manual button and a cron-scheduled action share one code
   path (the debcontrol "one action, three callers" rule, minus the REST
   API). If the version's level is within the acceptable level, the update
   runs. Otherwise it **stops**, fires `update.blocked` (with the AI's
   reason), and waits. Someone with `update.override` can allow it by
   hand, which is audit-logged. Results fire `update.succeeded` /
   `update.failed`.
4. **Execution** goes through the connector proxy, as far as the probe shows
   it is possible. Anything the proxy can't do stays read-only, with a
   GO/STOP recommendation instead.
