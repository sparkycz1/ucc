<p align="center">
  <img src="app/web/static/img/favicon.svg" alt="ucc" width="88">
</p>

<div align="center">

# ucc — UniFi Control Center

</div>

[![CI](https://github.com/sparkycz1/ucc/actions/workflows/ci.yml/badge.svg)](https://github.com/sparkycz1/ucc/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)
![Python](https://img.shields.io/badge/python-3.14-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/web-FastAPI-009688?logo=fastapi&logoColor=white)
![Task queue](https://img.shields.io/badge/task%20queue-Celery-37814A?logo=celery&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/db-PostgreSQL%2018-336791?logo=postgresql&logoColor=white)
![Docker Compose](https://img.shields.io/badge/deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white)
![Dependabot](https://img.shields.io/badge/dependabot-enabled-025E8C?logo=dependabot&logoColor=white)
![Security: bandit rules via ruff](https://img.shields.io/badge/security-ruff%20%2B%20pip--audit-4B8BBE)

A web application for safer UniFi updates across many consoles and sites.
Add a **Fabric** (a UniFi Site Manager API key) and ucc mirrors every
console behind it: UniFi OS versions, application versions (Network,
Protect, …) and every device's firmware, with what update is available.

**Where this is going** (see [wiki/UniFi-Integration.md](wiki/UniFi-Integration.md)):
an AI assessment of every new release — reading community.ui.com and
Reddit for reports of breakage — rates each version 1–5, and updates
(manual, or cron-scheduled) only proceed when the rating is within the
acceptable level you set; otherwise they stop, notify, and wait for a
person with `update.override` to allow them by hand.

Every page requires a login; access is controlled by custom roles (RBAC)
an admin defines, accounts can be restricted to specific Fabrics, and
accounts can authenticate locally, against LDAP, or via OIDC SSO, with
optional (or role-required) TOTP two-factor and passkeys. Every change is
recorded in a hash-chained audit log. There is deliberately no REST API —
everything happens in the web UI.

Forked from [debcontrol](https://github.com/sparkycz1/debcontrol): same
stack (FastAPI + htmx, PostgreSQL, Celery/Redis, Docker Compose), auth,
RBAC, audit, AI providers, scheduling and notifications.

**Full documentation lives in the [wiki](wiki/Home.md)** — technology
choices and the full security model ([Architecture](wiki/Architecture.md)),
every feature in detail ([Home](wiki/Home.md)'s feature table), reverse
proxy guides, and local development/testing
([Development](wiki/Development.md)). This file only covers getting a
fresh instance running.

## 🚀 Quick start (Docker)

**Recommended — one interactive script does everything:**

```bash
git clone https://github.com/sparkycz1/ucc.git
cd ucc
python3 scripts/setup.py
```

It generates every secret, asks a handful of questions (timezone, whether
to use the bundled Caddy reverse proxy, the
Administrator password — or auto-generates one — and the host port), then
brings the stack up and creates the first admin account for you. Full
details: [wiki/Installation.md](wiki/Installation.md).

**Manual setup**, if you'd rather configure everything by hand:

```bash
cp .env.example .env
python3 scripts/generate_secrets.py
```

Paste the printed values (`SECRET_KEY`, `ENCRYPTION_KEY`,
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`) into `.env`. Optionally set `TZ`
(e.g. `Europe/Prague`) — it's applied to every container and used by the
app to display timestamps in the UI in that timezone; defaults to UTC.
Then:

```bash
docker compose up -d --build
```

The app listens on `APP_PORT` (default `8080`, plain HTTP, all interfaces
by default — meant to sit behind a TLS-terminating reverse proxy; set
`APP_BIND_ADDRESS=127.0.0.1` in `.env` — no `docker-compose.yml` edit
needed — or firewall the port off if you don't want that).
Point your own nginx/Traefik/Caddy at it — see the reverse-proxy guides in
the wiki: [nginx](wiki/Reverse-Proxy-Nginx.md) ·
[Traefik](wiki/Reverse-Proxy-Traefik.md) ·
[Caddy (standalone)](wiki/Reverse-Proxy-Caddy.md). Or use the **bundled
Caddy** (automatic HTTPS via Let's Encrypt, TLS 1.3 only, HTTP/3): set
`DOMAIN` and `ACME_EMAIL` in `.env`, point that domain's DNS at this host,
open ports 80/tcp, 443/tcp and 443/udp, then
`docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build`
— see [wiki/Reverse-Proxy-Caddy.md](wiki/Reverse-Proxy-Caddy.md).

Then create the first administrator account yourself:

```bash
docker compose exec web python scripts/create_admin.py --username admin
```

> [!WARNING]
> The app speaks **plain HTTP only**. Always put TLS termination in front
> of it, and firewall its port off (or set `APP_BIND_ADDRESS=127.0.0.1` in
> `.env`) if you don't want it reachable directly.

See [wiki/Installation.md](wiki/Installation.md) for the full walkthrough
(environment variables, LDAP/OIDC setup, account recovery if you ever get
locked out) and [wiki/Installation.md#updating](wiki/Installation.md#updating)
for upgrading later (`./scripts/upgrade.sh`). See
[wiki/Development.md](wiki/Development.md) for running the test suite,
linting/type-checking, adding a migration, and other project conventions.

## 🔌 Checking what the connector proxy allows

Ubiquiti documents the Site Manager API as read-only; whether *updates*
can be triggered through its cloud connector proxy is undocumented.
`scripts/probe_connector.py` finds out against your own key (read-only
unless you explicitly pass `--upgrade-device <mac> --yes-really-upgrade`):

```bash
UNIFI_API_KEY=... python3 scripts/probe_connector.py
```

## 🔒 Security

See [.github/SECURITY.md](.github/SECURITY.md) — supported versions and how to report a vulnerability. Dependency updates and security alerts are automated via [Dependabot](.github/dependabot.yml).

## 📄 License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for noncommercial use; commercial use needs a separate agreement with the licensor
