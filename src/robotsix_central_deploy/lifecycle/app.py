"""FastAPI application — lifecycle REST server.

This module creates the FastAPI app, registers all routers, and provides
the main entry point.  Dependency factories, helpers, and the lifespan
are in ``deps.py``.  Endpoint handlers are organised by resource in the
``routers/`` subpackage.
"""

from __future__ import annotations

import re

import logging

from fastapi import FastAPI

try:
    from .csrf import GatewayAwareCSRFMiddleware

    _HAS_CSRF = True
except ImportError:
    _HAS_CSRF = False
    logging.getLogger(__name__).warning(
        "starlette-csrf not installed; CSRF middleware disabled"
    )

try:
    from secure import (
        ContentSecurityPolicy,
        CrossOriginOpenerPolicy,
        CrossOriginResourcePolicy,
        PermissionsPolicy,
        ReferrerPolicy,
        Secure,
        Server,
        StrictTransportSecurity,
        XContentTypeOptions,
        XFrameOptions,
    )
    from secure.middleware import SecureASGIMiddleware

    _HAS_SECURE = True
except ImportError:  # pragma: no cover — optional dep
    _HAS_SECURE = False

from .csrf import get_csrf_secret
from .deps import lifespan
from .error_handlers import register_error_handlers
from .models import ErrorDetail
from .rate_limiter import RateLimitMiddleware
from .routers.health import router as health_router
from .routers.services import router as services_router
from .routers.services_deploy import router as services_deploy_router
from .routers.services_config import router as services_config_router
from .routers.services_env import router as services_env_router
from .routers.system import router as system_router
from .routers.volumes import router as volumes_router
from .routers.caretaker import router as caretaker_router
from .routers.onboard import router as onboard_router
from .routers.claude_auth import router as claude_auth_router
from .routers.chat import router as chat_router
from .routers.chat_github import router as chat_github_router
from .routers.chat_preview import router as chat_preview_router
from .routers.chat_langfuse import router as chat_langfuse_router
from .settings_router import settings_router
from ..ui.router import router as ui_router

# URL patterns exempt from CSRF checks — these are API routes authenticated
# via X-API-Key / Basic-Auth headers (bearer-style, not vulnerable to CSRF)
# plus the login/logout endpoints which handle CSRF tokens manually.
_CSRF_EXEMPT_URLS: list[re.Pattern[str]] = [
    re.compile(r"^/health$"),
    re.compile(r"^/login$"),
    re.compile(r"^/logout$"),
    re.compile(r"^/services"),
    re.compile(r"^/settings$"),
    re.compile(r"^/system/"),
    re.compile(r"^/onboard"),
    re.compile(r"^/volumes"),
    re.compile(r"^/caretaker/"),
    re.compile(r"^/disk"),
    re.compile(r"^/chat/"),
    re.compile(r"^/claude-auth/"),
    re.compile(r"^/components/"),
]

app = FastAPI(
    title="robotsix-central-deploy Lifecycle API",
    version="0.1.0",
    description="Start, stop, restart, and inspect suite services.",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    responses={
        401: {
            "model": ErrorDetail,
            "description": "Unauthorized — invalid or missing credentials",
        },
    },
)

register_error_handlers(app)

app.add_middleware(RateLimitMiddleware)

if _HAS_CSRF:
    _initial_csrf_secret = get_csrf_secret("")
    app.add_middleware(
        GatewayAwareCSRFMiddleware,
        secret=_initial_csrf_secret,
        cookie_secure=True,
        cookie_httponly=True,
        cookie_samesite="lax",
        exempt_urls=_CSRF_EXEMPT_URLS,
    )

if _HAS_SECURE:
    # Mirrors Preset.BALANCED except for the CSP script directives: the
    # dashboard wires its buttons through inline onclick= attributes (both
    # in the template and in rows rendered by dashboard.js) and login.html
    # carries an inline <script>, so script-src needs 'unsafe-inline' and
    # script-src-attr must not be 'none' — BALANCED's script-src 'self' +
    # script-src-attr 'none' silently disabled every button in the UI.
    _csp = (
        ContentSecurityPolicy()
        .default_src("'self'")
        .base_uri("'self'")
        .font_src("'self'", "https:", "data:")
        .form_action("'self'")
        .frame_ancestors("'self'")
        .img_src("'self'", "data:")
        .object_src("'none'")
        .script_src("'self'", "'unsafe-inline'")
        .script_src_attr("'unsafe-inline'")
        .style_src("'self'", "https:", "'unsafe-inline'")
        .upgrade_insecure_requests()
    )
    secure_headers = Secure(
        coop=CrossOriginOpenerPolicy().same_origin(),
        corp=CrossOriginResourcePolicy().same_origin(),
        csp=_csp,
        hsts=StrictTransportSecurity().max_age(31536000).include_subdomains(),
        permissions=PermissionsPolicy().geolocation().microphone().camera(),
        referrer=ReferrerPolicy().strict_origin_when_cross_origin(),
        server=Server().set(""),
        xcto=XContentTypeOptions().nosniff(),
        xfo=XFrameOptions().sameorigin(),
    )
    app.add_middleware(SecureASGIMiddleware, secure=secure_headers)

app.include_router(ui_router)
app.include_router(settings_router)
app.include_router(health_router)
app.include_router(system_router)
app.include_router(volumes_router)
app.include_router(services_router)
app.include_router(services_deploy_router)
app.include_router(services_config_router)
app.include_router(services_env_router)
app.include_router(caretaker_router)
app.include_router(onboard_router)
app.include_router(claude_auth_router)
app.include_router(chat_router)
app.include_router(chat_github_router)
app.include_router(chat_preview_router)
app.include_router(chat_langfuse_router)

# Gateway router — MUST be registered last so its catch-all routes only
# match after every specific API route has been tried.
from ..gateway.router import gateway_router  # noqa: E402

app.include_router(gateway_router)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    import robotsix_config

    from .config import LifecycleConfig

    cfg = robotsix_config.load_config(LifecycleConfig)
    from ._logging import LOGGING_CONFIG

    uvicorn.run(
        "robotsix_central_deploy.lifecycle.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_config=LOGGING_CONFIG,
    )
