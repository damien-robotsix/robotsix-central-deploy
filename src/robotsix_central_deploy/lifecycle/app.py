"""FastAPI application — lifecycle REST server.

This module creates the FastAPI app, registers all routers, and provides
the main entry point.  Dependency factories, helpers, and the lifespan
are in ``deps.py``.  Endpoint handlers are organised by resource in the
``routers/`` subpackage.
"""

from __future__ import annotations

from fastapi import FastAPI

from .deps import lifespan
from .error_handlers import register_error_handlers
from .models import ErrorDetail
from .routers.health import router as health_router
from .routers.services import router as services_router
from .routers.system import router as system_router
from .routers.volumes import router as volumes_router
from .routers.caretaker import router as caretaker_router
from .routers.onboard import router as onboard_router
from .routers.claude_auth import router as claude_auth_router
from .settings_router import settings_router
from ..ui.router import router as ui_router

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

app.include_router(ui_router)
app.include_router(settings_router)
app.include_router(health_router)
app.include_router(system_router)
app.include_router(volumes_router)
app.include_router(services_router)
app.include_router(caretaker_router)
app.include_router(onboard_router)
app.include_router(claude_auth_router)

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
    uvicorn.run(
        "robotsix_central_deploy.lifecycle.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )
