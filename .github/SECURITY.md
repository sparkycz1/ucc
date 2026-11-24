# 🔒 Security Policy

## Supported versions

Only the **latest released version** (the most recent [tag/release](https://github.com/sparkycz1/ucc/releases)) is supported with security fixes. There are no maintained older branches — upgrade to the latest release before reporting an issue, and after a fix ships.

| Version | Supported |
|---|---|
| Latest release | ✅ |
| Anything older | ❌ |

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.** Instead, open a private draft security advisory: this repository's **Security** tab → **Advisories** → **Report a vulnerability** / **New draft security advisory**.

Include, if known:
- The affected version/commit.
- Steps to reproduce, or a proof of concept.
- The impact you'd expect (what an attacker could actually do).

## Scope

This is a self-hosted admin tool that stores UniFi Site Manager API keys and — through the Site Manager connector proxy — may trigger updates on real UniFi networks. The threat model and existing safeguards are documented in the wiki:

- [Architecture → Security essentials](https://github.com/sparkycz1/ucc/wiki/Architecture#-security-essentials) — CSRF, HTTP headers, startup validation, container hardening, secrets at rest, plus [Authentication & RBAC](https://github.com/sparkycz1/ucc/wiki/Authentication-RBAC) (roles, per-account Fabric scope) and [Audit Log](https://github.com/sparkycz1/ucc/wiki/Audit-Log).
- [UniFi Integration](https://github.com/sparkycz1/ucc/wiki/UniFi-Integration) — how API keys are validated, stored and never rendered back, and what reaches UniFi.
- [AI Assistant](https://github.com/sparkycz1/ucc/wiki/AI-Assistant) — its read-only tools, permission model, and the residual prompt-injection risk.

Dependency vulnerabilities are tracked automatically via [Dependabot](dependabot.yml) (alerts and security updates are enabled on this repository).
