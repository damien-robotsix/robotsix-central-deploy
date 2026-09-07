"""Tests for request-ID / correlation-ID tracking.

Covers the middleware (header read/generate + response echo + context
binding) and the structlog processor that stamps ``request_id`` onto JSON
log records.
"""

from __future__ import annotations

import json
import logging
import logging.config
import uuid

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from robotsix_central_deploy.lifecycle._logging import (
    LOGGING_CONFIG,
    add_request_id,
)
from robotsix_central_deploy.lifecycle.request_id_middleware import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    _request_id_ctx,
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

    def test_context_reset_after_request(self) -> None:
        client = TestClient(_build_app())
        client.get("/echo")
        # Outside any request the context var falls back to its default.
        assert get_request_id() is None


# ---------------------------------------------------------------------------
# Logging processor / integration
# ---------------------------------------------------------------------------


class TestRequestIdLogging:
    def test_processor_stamps_current_id(self) -> None:
        token = _request_id_ctx.set("abc-123")
        try:
            out = add_request_id(None, "info", {"event": "hi"})
        finally:
            _request_id_ctx.reset(token)
        assert out["request_id"] == "abc-123"

    def test_processor_null_outside_request(self) -> None:
        out = add_request_id(None, "info", {"event": "hi"})
        assert out["request_id"] is None

    def test_json_log_includes_request_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger().setLevel("INFO")
        logger = logging.getLogger("robotsix_central_deploy.test_request_id_middleware")

        token = _request_id_ctx.set("corr-999")
        try:
            logger.info("hello")
        finally:
            _request_id_ctx.reset(token)

        record = json.loads(capsys.readouterr().out.strip())
        assert record["request_id"] == "corr-999"
        assert record["event"] == "hello"
