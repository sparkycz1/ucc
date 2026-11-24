<p align="center">
  <img src="app/web/static/img/favicon.svg" alt="ucc" width="96">
</p>

<h1 align="center">ucc — UniFi Control Center</h1>

<p align="center">
  <b>UniFi updates across every console and site — only once the community says the release is safe.</b><br>
  One place for every UniFi OS, application and device firmware version behind your<br>
  Site Manager API keys, with AI-rated releases gating what gets updated.
</p>

<p align="center">
  <a href="https://github.com/sparkycz1/ucc/releases/latest"><img src="https://img.shields.io/github/v/release/sparkycz1/ucc?color=006fff" alt="Latest release"></a>
  <a href="https://github.com/sparkycz1/ucc/actions/workflows/ci.yml"><img src="https://github.com/sparkycz1/ucc/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/sparkycz1/ucc/actions/workflows/codeql.yml"><img src="https://github.com/sparkycz1/ucc/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/sparkycz1/ucc/actions/workflows/fuzz.yml"><img src="https://github.com/sparkycz1/ucc/actions/workflows/fuzz.yml/badge.svg" alt="Fuzz"></a>
  <a href="https://github.com/sparkycz1/ucc/actions/workflows/scorecard.yml"><img src="https://github.com/sparkycz1/ucc/actions/workflows/scorecard.yml/badge.svg" alt="Scorecard"></a>
  <a href="https://github.com/sparkycz1/ucc/actions/workflows/release.yml"><img src="https://github.com/sparkycz1/ucc/actions/workflows/release.yml/badge.svg" alt="Release"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/sparkycz1/ucc"><img src="https://api.securityscorecards.dev/projects/github.com/sparkycz1/ucc/badge" alt="OpenSSF Scorecard"></a>
  <a href="https://github.com/sparkycz1/ucc/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue" alt="License"></a>
</p>

<p align="center">
  <a href="https://github.com/sparkycz1/ucc/blob/main/.github/SECURITY.md"><img src="https://img.shields.io/badge/security-policy-blue" alt="Security policy"></a>
  <a href="https://github.com/sparkycz1/ucc/security"><img src="https://img.shields.io/badge/secret%20scanning-enabled-brightgreen" alt="Secret scanning"></a>
  <a href="https://github.com/dependabot/dependabot-core"><img src="https://img.shields.io/badge/dependabot-enabled-025E8C?logo=dependabot&logoColor=white" alt="Dependabot"></a>
  <a href="https://github.com/pypa/pip-audit"><img src="https://img.shields.io/badge/pip--audit-in%20CI-4B8BBE" alt="pip-audit"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="https://github.com/python/mypy"><img src="https://img.shields.io/badge/mypy-strict-2A6DB2" alt="mypy strict"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
</p>

<p align="center">
  <a href="https://github.com/python/cpython"><img src="https://img.shields.io/badge/python-3.15-3776AB?logo=python&logoColor=white" alt="Python 3.15"></a>
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://github.com/bigskysoftware/htmx"><img src="https://img.shields.io/badge/htmx-3366CC?logo=htmx&logoColor=white" alt="htmx"></a>
  <a href="https://github.com/sqlalchemy/sqlalchemy"><img src="https://img.shields.io/badge/SQLAlchemy%202.0-D71F00?logo=sqlalchemy&logoColor=white" alt="SQLAlchemy 2.0"></a>
  <a href="https://github.com/postgres/postgres"><img src="https://img.shields.io/badge/PostgreSQL%2018-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 18"></a>
  <a href="https://github.com/redis/redis"><img src="https://img.shields.io/badge/Redis%208-DC382D?logo=redis&logoColor=white" alt="Redis 8"></a>
  <a href="https://github.com/celery/celery"><img src="https://img.shields.io/badge/Celery-37814A?logo=celery&logoColor=white" alt="Celery"></a>
  <a href="https://github.com/docker/compose"><img src="https://img.shields.io/badge/Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose"></a>
  <a href="https://github.com/caddyserver/caddy"><img src="https://img.shields.io/badge/Caddy-1F88C0?logo=caddy&logoColor=white" alt="Caddy"></a>
  <a href="https://github.com/sparkycz1/ucc/tree/main/app/i18n/locales"><img src="https://img.shields.io/badge/i18n-English%20%7C%20%C4%8Ce%C5%A1tina-006fff" alt="English and Czech"></a>
</p>

<p align="center">
  <a href="#-quick-start"><b>Quick start</b></a> ·
  <a href="https://github.com/sparkycz1/ucc/wiki"><b>Documentation</b></a> ·
  <a href="https://github.com/sparkycz1/ucc/wiki/UniFi-Integration"><b>UniFi integration</b></a> ·
  <a href="https://github.com/sparkycz1/ucc/releases"><b>Releases</b></a> ·
  <a href="https://github.com/sparkycz1/ucc/blob/main/.github/SECURITY.md"><b>Security</b></a>
</p>

## ✨ What it does

| | |
|---|---|
| 🧵 **Fabrics** | Add a UniFi Site Manager API key and ucc mirrors every console behind it: UniFi OS, Network/Protect/… versions, sites, and every device's firmware — with the available update. Synced every 15 minutes, on demand, or on a cron schedule. |
| 🧠 **AI-rated releases** *(next phase)* | Each new version is read up on community.ui.com and Reddit and rated 1 (stable) … 5 (broken), with the reasons and links to the posts. |
| ⬆️ **Gated updates** *(next phase)* | Manual or cron-scheduled updates run only when the rating is within the level you accept — global, per Fabric, site or device type. Otherwise they stop, notify, and wait for someone allowed to override. |
| ⏱️ **Scheduling** | Cron any action against a site, a Fabric or every Fabric, with run history. |
| 🔔 **Notifications** | Email or webhook on a failed sync, a finished assessment, a blocked, successful or failed update — per Fabric, per role. |
| 🧠 **AI assistant** | Ask about your inventory in plain language. Read-only, scoped to the Fabrics you may see. |
| 📝 **Audit log** | Every change, hash-chained, exportable, forwardable to your SIEM. |

## 💡 Why ucc

- 🔌 **Nothing to open.** ucc only talks to UniFi Site Manager
  (`api.ui.com`). No VPN, port forward or agent on any site.
- 🛡️ **Safe by default.** API keys are validated on entry, encrypted
  with AES-256-GCM and never shown again; a strict Content Security
  Policy; every change goes to a hash-chained audit log.
- 👥 **Built for a team.** Custom roles, per-account Fabric scope, LDAP,
  OIDC SSO, TOTP and passkeys.
- 🌍 **Your language.** Every page in English and Czech. A new language is
  one JSON file.

There is deliberately **no REST API** — everything happens in the web UI,
behind a session and CSRF protection.

## 🚀 Quick start

You need Docker with Compose v2.

```bash
git clone https://github.com/sparkycz1/ucc.git
cd ucc
python3 scripts/setup.py
```

The wizard generates every secret, asks a few questions (time zone,
language, the bundled Caddy for HTTPS, the admin password, the port),
starts the stack and creates the first administrator. Then open
`http://<host>:8080`, go to **Fabrics → Add Fabric** and paste a Site
Manager API key (unifi.ui.com → API).

> [!WARNING]
> The app itself speaks **plain HTTP**. Put TLS in front of it: the
> bundled [Caddy](https://github.com/sparkycz1/ucc/wiki/Reverse-Proxy-Caddy)
> (automatic Let's Encrypt), or your own
> [nginx](https://github.com/sparkycz1/ucc/wiki/Reverse-Proxy-Nginx) or
> [Traefik](https://github.com/sparkycz1/ucc/wiki/Reverse-Proxy-Traefik).

<details>
<summary><b>Manual setup instead of the wizard</b></summary>

```bash
cp .env.example .env
python3 scripts/generate_secrets.py   # paste the printed values into .env
docker compose up -d --build
docker compose exec web python scripts/create_admin.py --username admin
```

With the bundled Caddy: set `DOMAIN` and `ACME_EMAIL` in `.env`, point
the domain's DNS at this host, open ports 80/tcp, 443/tcp and 443/udp,
then `docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build`.

Every variable is explained in
[Installation](https://github.com/sparkycz1/ucc/wiki/Installation).
</details>

**Updating** later: `./scripts/upgrade.sh`
([how](https://github.com/sparkycz1/ucc/wiki/Installation#updating)).

### 🔌 What can the connector proxy do?

Site Manager API keys are documented as read-only; whether updates can be
triggered through the cloud connector proxy is undocumented.
[`scripts/probe_connector.py`](scripts/probe_connector.py) finds out
against your own key — read-only unless you explicitly pass
`--upgrade-device <mac> --yes-really-upgrade`:

```bash
UNIFI_API_KEY=... python3 scripts/probe_connector.py
```

## 📚 Documentation

Everything lives in the **[wiki](https://github.com/sparkycz1/ucc/wiki)**:

- **Set it up**: [Installation](https://github.com/sparkycz1/ucc/wiki/Installation) ·
  [Host requirements](https://github.com/sparkycz1/ucc/wiki/Host-Requirements)
- **Use it**: [every feature, page by page](https://github.com/sparkycz1/ucc/wiki/Home) ·
  [UniFi integration](https://github.com/sparkycz1/ucc/wiki/UniFi-Integration)
- **Understand or change it**: [Architecture and security model](https://github.com/sparkycz1/ucc/wiki/Architecture) ·
  [Development](https://github.com/sparkycz1/ucc/wiki/Development)

## 🔒 Security

Found a vulnerability? Please report it privately, see
[SECURITY.md](.github/SECURITY.md). CI runs CodeQL, pip-audit,
dependency review and fuzzing on every change; Dependabot keeps dependencies current.

## 🤝 Contributing

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).
ucc is forked from [debcontrol](https://github.com/sparkycz1/debcontrol)
and, like it, much of it was written with AI assistance.

## 📄 License

[PolyForm Noncommercial 1.0.0](LICENSE): free for noncommercial use.
Commercial use needs a separate agreement with the licensor.
