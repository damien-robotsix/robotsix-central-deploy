"""Tests for request-ID / correlation-ID tracking.

Covers the middleware (header read/generate + response echo + context
binding) and the ``get_request_id`` accessor that reads the id bound onto
``structlog.contextvars``.
"""

from __future__ import annotations

import uuid

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from robotsix_central_deploy.lifecycle.request_id_middleware import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    get_request_id,
)


def _build_app() -> Starlette:
    """A minimal app that echoes the bound request id in its JSON body."""

    async def endpoint(request):  # type: ignore[no-untyped-def]
        return JSONResponse({"seen_request_id": get_request_id()})

    app = Starlette(routes=[Route("/echo", endpoint)])
    app.add_middleware(RequestIDMiddleware)
    return app


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TestRequestIDMiddleware:
    def test_generates_id_when_header_absent(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo")
        header_id = resp.headers[REQUEST_ID_HEADER]
        # Valid UUID4 and matches the value seen inside the request.
        uuid.UUID(header_id)
        assert resp.json()["seen_request_id"] == header_id

    def test_reuses_client_supplied_header(self) -> None:
        client = TestClient(_build_app())
        supplied = "550e8400-e29b-41d4-a716-446655440000"
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: supplied})
        assert resp.headers[REQUEST_ID_HEADER] == supplied
        assert resp.json()["seen_request_id"] == supplied

    def test_blank_header_is_replaced_with_generated_id(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: "   "})
        header_id = resp.headers[REQUEST_ID_HEADER]
        uuid.UUID(header_id)
        assert resp.json()["seen_request_id"] == header_id

    def test_distinct_ids_across_requests(self) -> None:
        client = TestClient(_build_app())
        first = client.get("/echo").headers[REQUEST_ID_HEADER]
        second = client.get("/echo").headers[REQUEST_ID_HEADER]
        assert first != second

    def test_context_cleared_after_request(self) -> None:
        client = TestClient(_build_app())
        client.get("/echo")
        # Outside any request the id is unbound.
        assert get_request_id() is None
