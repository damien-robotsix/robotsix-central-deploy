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
            content = ErrorDetail(error=exc.detail, detail="").model_dump()
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
