# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ucc (UniFi Control Center): a FastAPI + htmx web app for managing UniFi
firmware/application updates across many consoles and sites. A **Fabric**
is one UniFi Site Manager API key; ucc syncs its consoles (UniFi OS and app
versions), sites and devices (firmware), and — next phase — gates updates
on an AI assessment of each release (community.ui.com + Reddit, rated 1–5)
against an acceptable level. Forked from debcontrol (SSH machine
management): auth, RBAC, audit, AI providers, scheduling, notifications,
i18n and the Docker setup are inherited; machines/SSH and the REST API are
gone. **There is deliberately no REST API** — web UI only.
Server-rendered Jinja2 + htmx, not a SPA. Python 3.14, SQLAlchemy 2.0
async + PostgreSQL, Celery + Redis for background work, deployed via
Docker Compose only (no supported bare-metal/venv run path for the app
itself).

**The wiki is the primary source of truth, not this file** — it is
detailed, current, and actively maintained alongside the code:

- **[wiki/Development.md](wiki/Development.md)** — commands, testing
  conventions, and step-by-step recipes for the most common changes
  (adding a route, a permission, a scheduled action, a background task,
  an audit log call). Read the relevant recipe before adding one of
  these rather than improvising a new pattern.
- **[wiki/Architecture.md](wiki/Architecture.md)** — stack, project
  structure, and cross-cutting security essentials (CSRF/CSP/headers,
  startup validation, container hardening). Feature-specific depth lives
  in its own page from there: **[Authentication &
  RBAC](wiki/Authentication-RBAC.md)** (logins, sessions, permissions,
  Fabric scope), **[UniFi Integration](wiki/UniFi-Integration.md)**
  (Fabrics, inventory sync, connector proxy, the AI-gated update roadmap),
  **[Audit Log](wiki/Audit-Log.md)**, and
  **[Notifications](wiki/Notifications.md)** (rules, templates, every
  placeholder). Read the relevant page before changing something it
  documents, rather than guessing from the code alone.
- **[wiki/Home.md](wiki/Home.md)** — the feature table (what every page
  does), the canonical wiki table of contents.

When a task touches something these already document, follow the existing
pattern and update the relevant wiki page in the same change — don't let
docs drift from code (a completeness audit is what closed several such
gaps in this repo's history; see recent commits).

## Commands

```bash
uv sync                          # install deps into .venv (needed for tests/lint/mypy)
uv run pytest                    # full suite — no real Postgres/Redis/Celery broker touched
uv run pytest tests/test_x.py    # one file
uv run pytest tests/test_x.py::test_name   # one test
uv run ruff check .              # lint
uv run mypy app alembic tests    # type check (strict for app/ and alembic/)
uv run alembic revision --autogenerate -m "..."   # after changing a model — READ the generated file
uv run alembic upgrade head
uv run alembic heads             # must show exactly one head before committing a migration
```

There is no supported way to run the app itself outside Docker:
`docker compose up -d --build` (or `python3 scripts/setup.py` for a guided
first-time setup). See [wiki/Installation.md](wiki/Installation.md).

**Before committing**, run the same gate this repo's history consistently
uses: `ruff check .`, `mypy app alembic tests`, `pytest`, `alembic heads`
(single head) — all clean.

**Every round of changes** bumps `APP_VERSION` in `app/core/version.py`
**and** `version` in `pyproject.toml` together (patch for a small fix,
minor for a feature/infrastructure change, major only if explicitly
asked) — then run `uv lock` and commit the updated `uv.lock` in the same
commit. Forgetting the `uv lock` step breaks `uv sync --locked` in the
Docker build for anyone on a fresh install (this happened in debcontrol's
history around v0.19.0/v0.19.1).

## Architecture, beyond what one file shows

- **The async/sync seam.** App logic is async all the way down
  (SQLAlchemy async sessions, httpx), but Celery tasks are synchronous.
  Every background job in `app/tasks/jobs.py` is `async def _do_thing(...)`
  (the real work) plus a one-line `def do_thing(...): return
  asyncio.run(_do_thing(...))` wrapped in `@celery_app.task(name="...")`.
  Tests call the underscore-prefixed coroutine directly, never the sync
  wrapper (which can't run inside pytest-asyncio's already-running loop).
- **Fork safety.** Celery workers fork; a `from app.db.session import
  AsyncSessionLocal` name captured at import time in the parent process
  keeps pointing at the parent's (now-invalid) connection pool in a forked
  child. Task code always opens sessions as `db_session.AsyncSessionLocal()`
  through the module (`from app.db import session as db_session`), never
  via a direct binding.
- **One action, two callers, one code path.** An action against a
  Fabric/site (inventory sync today; updates next) is called identically by
  a human's button click (`app/web/routes/fabrics.py`) and a cron-scheduled
  task (`app/scheduling/`) — never reimplemented per caller. Site Manager
  access goes through `app/unifi/client.py` only.
- **No REST API.** There is no `/api/` surface, no API tokens and no
  Swagger UI — don't add a JSON endpoint as a side door. `/api/` is not on
  the auth middleware's public allowlist.
- **Fabric scope is the visibility boundary.** Anything listing or
  resolving a Fabric — or anything synced under one — for a specific user
  goes through `app.services.access_scope` (web routes and AI tools alike).
  Out of scope is a 404, never a 403.
- **WebSockets do their own auth.** `app.auth.middleware` never runs for
  `scope["type"] == "websocket"` requests (Starlette only invokes
  `http`-scoped middleware for those) — `live_ws.py` re-implements the
  session-cookie + permission + Fabric-scope check by hand at the top of
  the handler, before `accept()`ing the connection.
- **Live updates are a doorbell, not a data feed.** `app/services/
  live_updates.py` publishes `{"kind": "..."}` over Redis pub/sub after a
  background job's commit; `app/web/routes/live_ws.py` relays it to the
  browser; `static/js/live-updates.js` turns it into a `live-<kind>` DOM
  event an htmx panel's `hx-trigger` listens for, re-triggering that
  panel's own already-permission-checked fetch. The push never carries
  inventory data itself.
- **CSP is strict — no inline scripts or styles, no CDN.** `script-src
  'self'`, `style-src 'self'`. Every third-party JS/CSS bundle (htmx) is
  vendored under `app/web/static/`, not loaded
  from a CDN. A CSP violation is silent at the Python layer (route returns
  200, tests pass) and only shows up as a blank widget + console error in
  a real browser — verify anything CSP-adjacent there, not just by reading
  the code. New CSS reads colors through `--color-*` custom properties in
  `style.css` (never a hardcoded color), since the light theme
  (`:root[data-theme="light"]`) only overrides those variables.
- **Audit logging is a deliberate, curated trail, not a debug log.**
  `app.audit.log_event(...)` is called once, after the triggering
  `db.commit()`, for every human- or schedule-initiated mutation and for
  each safeguard that blocked one (a rejected API key, a blocked update) —
  never for the unattended periodic sweeps themselves (the inventory sync;
  only its healthy-to-failing transition is logged). Action codes are
  `lowercase.dot.separated`.
- **Model discovery for Alembic** happens via explicit imports in
  `app/db/models/__init__.py` and `alembic/env.py` — a new model module
  not imported there is invisible to `--autogenerate`.
- **UI strings go through `t()`, not literal English in a template.**
  `{{ t(request, "area.key") }}` (`app/web/templating.py`, backed by
  `app/i18n/`) resolves per-account — see [Per-user UI language](wiki/Authentication-RBAC.md#per-user-ui-language-i18n).
  Not every page is converted yet; a page that already uses `t()` should
  stay consistent, and any new key needs an entry in **every** file under
  `app/i18n/locales/`, not just `en.json` (see the checklist below).

## Checklist for every change

Before considering a change finished, not just "the code works":

1. **No REST API.** A new capability is web-UI-only (see above). If it
   ever needs machine-to-machine access, that is a design decision to raise
   with the user, not something to add in passing.
2. **Wiki parity.** Update the relevant `wiki/*.md` page(s) in the same
   change — `wiki/Home.md`'s feature table, `wiki/Architecture.md` for
   *why*/how it works, `wiki/Development.md` if it adds a new pattern
   worth a recipe. Code and docs drifting apart is treated as a bug here,
   not a nice-to-have (see git history — a dedicated audit pass exists
   specifically because this had already happened).
3. **i18n parity.** Any new or changed user-facing string goes through
   `t(request, "...")` and gets a key in `app/i18n/locales/en.json` *and*
   every other locale file already shipping (`cs.json` today) — not just
   the one you're thinking in. A key with no translation in a given locale
   is acceptable (it falls back to English) only when that whole locale is
   still catching up, not as a way to skip translating a key you just
   added everywhere else.
4. **Upgrade safety.** Assume a real instance is already running this app
   in production with existing data and is about to `git pull` +
   `docker compose up -d --build` straight onto whatever you just wrote —
   never something spun up fresh. A new column is nullable or has a safe
   server default (see the `User.locale` migration for the pattern); a
   renamed/removed route, permission, config key, or task name breaks
   someone silently unless it's kept working (redirected, aliased,
   deprecated-with-warning) or the change is flagged loudly as breaking.
   Every existing feature must keep working after the change, not just the
   new one.
5. **Security.** This is admin tooling holding API keys that can reach
   (and, via the connector proxy, possibly update) real UniFi networks —
   CSRF on every mutating web route, the exact matching `Permission` on
   both the web and API side, secrets only ever `encrypt_secret`/stored
   hashed, no new inline script/style (CSP), no new trust boundary crossed
   without the same scrutiny `wiki/Architecture.md`'s security model
   section already applies elsewhere.
6. **Current, not legacy, tech.** Match what's already here (Python 3.14,
   SQLAlchemy 2.0 async, Pydantic v2, FastAPI, htmx 2.x) — don't introduce
   an older pattern (sync SQLAlchemy, Pydantic v1 style, a jQuery-era JS
   habit) because it's more familiar; if a genuinely better modern option
   exists for a *new* piece of work, prefer it over copying an older
   pattern just for consistency's sake.
7. **Scale.** Fabrics can hold thousands of devices, and Site Manager
   rate-limits (100 requests/min per console through the connector). A new
   per-Fabric/per-console fan-out belongs in the "enqueue one Celery task
   per target, never await them inline" pattern (`app/tasks/jobs.py`), and
   a new query should scale with an index, not a full-table scan.
8. **Tag and release.** Once `APP_VERSION`/`pyproject.toml` are bumped and
   the change is committed and pushed, tag it (`git tag vX.Y.Z` + `git push
   --tags`) and cut a GitHub release (`gh release create vX.Y.Z`) — a
   version bump that never becomes a tag/release is invisible to
   `scripts/upgrade.sh` and to anyone reading the Releases page to see
   what changed. Don't batch several version bumps into one eventual tag;
   each `APP_VERSION` that lands on `main` gets its own.

None of this means doing every possible thing for every tiny change —
it means actually checking each of these against what you just did,
the same way you'd check the test/lint/mypy gate, and either handling it
or explicitly deciding (and saying) it doesn't apply this time.
