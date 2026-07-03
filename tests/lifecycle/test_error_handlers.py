"""Tests for centralized error handlers.

Exercises the ``register_error_handlers()`` machinery: validation
errors return a structured ``ErrorDetail`` body at 422, HTTP
exceptions return the same shape with the status code they specify,
and unhandled exceptions return a safe generic 500.
"""

from __future__ import annotations

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.models import ErrorDetail


class TestErrorHandlerRegistration:
    """Unit-level assertions on the ``app.exception_handlers`` dict."""

    def test_handlers_are_registered(self):
        handlers = server_mod.app.exception_handlers
        assert handlers, "exception_handlers dict should not be empty"

        # The handler keys are the exception *types*.
        handler_types = {exc for exc in handlers}
        # We expect at least StarletteHTTPException, RequestValidationError, and Exception.
        from starlette.exceptions import HTTPException as StarletteHTTPException
        from fastapi.exceptions import RequestValidationError

        assert StarletteHTTPException in handler_types, (
            f"StarletteHTTPException not in {handler_types}"
        )
        assert RequestValidationError in handler_types, (
            f"RequestValidationError not in {handler_types}"
        )
        assert Exception in handler_types, f"Exception not in {handler_types}"


class TestValidationError:
    """RequestValidationError → 422 + ErrorDetail envelope."""

    async def test_missing_required_body_field(self, client: AsyncClient, auth_headers):
        """POST /onboard/confirm with an empty body should trigger validation."""
        resp = await client.post(
            "/onboard/confirm",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "error" in body, f"missing 'error' key in {body}"
        assert "detail" in body, f"missing 'detail' key in {body}"
        assert body["error"] == "Request validation failed"
        assert isinstance(body["detail"], list)
        # FastAPI validation detail is an array of error objects.
        assert len(body["detail"]) > 0
        for err in body["detail"]:
            assert "loc" in err
            assert "msg" in err
            assert "type" in err

    async def test_invalid_type_in_body(self, client: AsyncClient, auth_headers):
        """Sending a string where an object is expected triggers 422."""
        resp = await client.post(
            "/onboard/confirm",
            content=b"not-json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"] == "Request validation failed"
        assert isinstance(body["detail"], list)


class TestUnhandledException:
    """Exception → 500 with safe, no-leak ErrorDetail."""

    async def test_unhandled_exception_returns_500(self, auth_headers, monkeypatch):
        """Force an unexpected exception and verify a safe 500 response."""
        from httpx import ASGITransport, AsyncClient

        # Create a client that doesn't re-raise ASGI exceptions — we want
        # to see the 500 response, not the raw exception.
        transport = ASGITransport(
            app=server_mod.app,
            raise_app_exceptions=False,  # type: ignore[arg-type]
        )
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            store = server_mod.app.state.store
            original_list_all = store.list_all

            async def _boom():
                raise RuntimeError("simulated database failure")

            monkeypatch.setattr(store, "list_all", _boom)

            resp = await client.get("/services", headers=auth_headers)

            # Restore for other tests.
            monkeypatch.setattr(store, "list_all", original_list_all)

        assert resp.status_code == 500, f"got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"] == "Internal server error"
        assert body["detail"] == ""
        # The raw exception must not leak.
        assert "simulated" not in str(body)
        assert "RuntimeError" not in str(body)
        assert "traceback" not in str(body).lower()


class TestHTTPExceptionEnvelope:
    """Existing HTTPException handler still produces ErrorDetail shape."""

    async def test_404_produces_error_detail(self, client: AsyncClient, auth_headers):
        """A 404 from a missing service should match the ErrorDetail shape."""
        resp = await client.get("/services/nonexistent-zzz", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        # Validate it matches the ErrorDetail shape
        _ = ErrorDetail(**body)
        assert "error" in body
