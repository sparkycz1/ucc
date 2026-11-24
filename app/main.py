"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth.middleware import require_auth
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.proxy_headers import ProxyHeadersMiddleware
from app.core.version import APP_VERSION
from app.db.session import AsyncSessionLocal
from app.scheduling.builtin_actions import register_builtin_actions
from app.web.routes import (
    ai,
    audit,
    auth,
    branding,
    dashboard,
    fabrics,
    impersonation,
    live_ws,
    notifications,
    releases,
    roles,
    scheduling,
    theme,
    users,
)
from app.web.routes import settings as settings_routes

settings = get_settings()
configure_logging(settings.log_level)
# Populates app.scheduling.actions' registry — the "New scheduled task" form
# reads from it. Idempotent, and also called from app.scheduling.jobs (and
# again in each forked Celery worker child) so the worker processes have it
# too without needing to import this module.
register_builtin_actions()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"

# Strict CSP: no inline scripts/styles, no external CDN (htmx is vendored
# locally). `connect-src 'self'` covers the same-origin live-updates WebSocket.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # One long-lived Redis connection pool for the *login rate limiter*
    # (app.auth.rate_limit) — nothing to do with the task queue, which is
    # Celery and talks to Redis from the worker processes on its own. Kept
    # here so a burst of login attempts doesn't open a fresh connection per
    # request.
    app.state.redis = aioredis.from_url(settings.redis_url)
    # The auth middleware (app.auth.middleware) needs a DB session but runs
    # outside FastAPI's dependency injection — this is what it opens one
    # from. Kept on app.state (like `redis` above) rather than imported
    # directly so tests can point it at their own SQLite engine instead of
    # the real Postgres one `AsyncSessionLocal` is bound to.
    app.state.db_session_factory = AsyncSessionLocal
    try:
        yield
    finally:
        await app.state.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ucc",
        description="UniFi Control Center — AI-gated UniFi update management.",
        version=APP_VERSION,
        lifespan=lifespan,
        # No REST API in this app: no Swagger UI, no OpenAPI schema endpoint.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Registered first so it ends up outermost (see the `_require_auth`
    # comment below for why registration order maps to layering here) —
    # every other middleware, and every route/WebSocket handler, needs to
    # see the corrected scheme, not just the ones that happen to run after
    # some other check. See app.core.proxy_headers's own module docstring
    # for what this fixes and why trusting it is safe.
    app.add_middleware(
        ProxyHeadersMiddleware,
        trust_all=settings.trust_all_proxies,
        trusted_networks=settings.trusted_proxy_networks,
        trust_forwarded_for=settings.trust_forwarded_for,
    )

    # Starlette's own session middleware — used *only* to carry OIDC's
    # `state`/`nonce` across the redirect to/from the provider (Authlib
    # reads/writes `request.session` itself). Unrelated to the app's own
    # login sessions (app.auth.sessions, a DB row + a separate cookie) —
    # this one is short-lived (only needs to survive one redirect round
    # trip) and never holds anything that grants access by itself.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="oidc_flow",
        same_site="lax",  # "strict" would drop this cookie on the provider's redirect back.
        https_only=settings.is_production,
        max_age=600,
    )

    # Registered before `security_headers` below so that middleware ends up
    # *outermost* (Starlette wraps middleware in reverse registration order)
    # — meaning it still gets to add CSP/etc. headers to a response
    # `require_auth` returns directly (a redirect to /login), not just to
    # ones that reached a route.
    @app.middleware("http")
    async def _require_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        return await require_auth(request, call_next)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    app.include_router(branding.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(fabrics.router)
    app.include_router(releases.router)
    app.include_router(scheduling.router)
    app.include_router(audit.router)
    app.include_router(ai.router)
    app.include_router(users.router)
    app.include_router(impersonation.router)
    app.include_router(roles.router)
    app.include_router(notifications.router)
    app.include_router(settings_routes.router)
    app.include_router(theme.router)
    # No HTTP dependency here — WebSocket connections never go through
    # `app.auth.middleware`, so this router does its own auth inside the
    # handler. See live_ws.py's module docstring.
    app.include_router(live_ws.router)

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        return RedirectResponse(url="/dashboard")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
