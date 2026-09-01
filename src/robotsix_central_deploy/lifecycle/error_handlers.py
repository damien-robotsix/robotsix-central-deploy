"""Centralized FastAPI exception handlers.

Registers handlers for HTTP exceptions, Pydantic validation errors,
and a catch-all for unhandled exceptions — all returning the
``ErrorDetail`` response shape.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models import ErrorDetail

logger = logging.getLogger(__name__)

#: Starlette's detail for a path that matched no route.  Only these bare 404s
#: get enriched with route hints — an explicit ``HTTPException`` (e.g. the
#: ``did you mean …?`` 404 from ``deps/dependencies.py``) carries its own
#: message and must be left untouched.
_ROUTE_NOT_FOUND_DETAIL = "Not Found"

#: Per-service sub-paths the lifecycle router actually serves.  Named in the
#: 404 body so an agent that guessed a wrong path (``/redeploy``, ``/status``)
#: sees the real routes instead of a bare "Not Found" and wrongly concludes
#: the control plane cannot do it.
_SERVICE_ROUTES = (
    "GET /services/{name} (state, image, health, digests)",
    "POST /services/{name}/deploy",
    "POST /services/{name}/rollback",
    "POST /services/{name}/start",
    "POST /services/{name}/stop",
    "POST /services/{name}/restart",
    "POST /services/{name}/refresh-contract",
    "GET /services/{name}/logs",
    "GET|PUT /services/{name}/env",
    "GET /services/{name}/health",
    "GET /services/{name}/history",
    "DELETE /services/{name}",
)

#: GET actions an agent invents for a service's current state — all served by
#: the bare ``GET /services/{name}`` under a different name.
_STATE_ALIASES = frozenset({"status", "state", "info", "describe"})


def _service_route_hint(method: str, path: str) -> str | None:
    """Explain the valid per-service routes when *path* guessed a wrong one.

    A ``404`` for ``/services/{name}/<unknown>`` otherwise reads as "this
    plane cannot do it" when the real route merely has a different name.
    Returns ``None`` for any path that is not a per-service sub-path, so the
    caller falls back to the plain message.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "services":
        return None
    name = parts[1]
    action = "/".join(parts[2:])
    routes = ", ".join(r.replace("{name}", name) for r in _SERVICE_ROUTES)
    hint = (
        f"No route '{method} /services/{name}/{action}'. "
        f"Valid per-service routes: {routes}."
    )
    if method == "GET" and action in _STATE_ALIASES:
        hint += f" For current state use GET /services/{name}."
    return hint


def register_error_handlers(app: FastAPI) -> None:
    """Register structured error handlers on *app*."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if isinstance(exc.detail, dict):
            content = dict(exc.detail)
            content.setdefault("error", str(exc.detail))
            content.setdefault("detail", "")
        elif isinstance(exc.detail, str):
            error_text = exc.detail
            if exc.status_code == 404 and exc.detail == _ROUTE_NOT_FOUND_DETAIL:
                hint = _service_route_hint(request.method, request.url.path)
                if hint is not None:
                    error_text = hint
            content = ErrorDetail(error=error_text, detail="").model_dump()
        else:
            content = ErrorDetail(error=str(exc.detail), detail="").model_dump()
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=exc.headers if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorDetail(
                error="Request validation failed",
                detail=jsonable_encoder(exc.errors()),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorDetail(
                error="Internal server error",
                detail="",
            ).model_dump(),
        )
