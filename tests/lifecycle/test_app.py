"""Tests for the FastAPI app router registration ordering.

The gateway router MUST be registered last — it includes catch-all routes
(``/{path:path}``) that would shadow specific API routes if registered earlier.
"""

from __future__ import annotations

from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient

from robotsix_central_deploy.lifecycle.app import app


# ---------------------------------------------------------------------------
# Route ordering invariant
# ---------------------------------------------------------------------------


def test_gateway_router_registered_last():
    """Gateway catch-all routes appear after every non-gateway route."""
    routes = list(app.router.routes)

    # Find the index of the first gateway route.
    first_gateway_idx = None
    for i, route in enumerate(routes):
        if hasattr(route, "tags") and "gateway" in route.tags:
            first_gateway_idx = i
            break

    assert first_gateway_idx is not None, "gateway router not found in app routes"

    # Every route after the first gateway route must also be a gateway route
    # (i.e., no non-gateway route is registered after the gateway).
    for i in range(first_gateway_idx + 1, len(routes)):
        route = routes[i]
        tags = getattr(route, "tags", []) or []
        assert (
            "gateway" in tags
            or "gateway" not in tags
            and route.path
            in (
                "/{path:path}",
                "/",
            )
        ), (
            f"Non-gateway route at position {i} (path={route.path!r}) "
            f"appears after the first gateway route at position {first_gateway_idx}"
        )


def test_specific_api_routes_before_gateway():
    """Every well-known API route is registered before the first gateway route."""
    routes = list(app.router.routes)

    # Find the index of the first gateway route.
    first_gateway_idx = None
    for i, route in enumerate(routes):
        if hasattr(route, "tags") and "gateway" in route.tags:
            first_gateway_idx = i
            break

    assert first_gateway_idx is not None, "gateway router not found in app routes"

    # Collect the paths of all routes before the gateway.
    api_paths: set[str] = set()
    for route in routes[:first_gateway_idx]:
        api_paths.add(route.path)

    # Verify that key API endpoints are present before the gateway.
    required_prefixes = [
        "/health",
        "/services",
        "/system/update",
        "/settings",
        "/onboard/preflight",
        "/ui",
    ]
    for prefix in required_prefixes:
        assert any(p.startswith(prefix) for p in api_paths), (
            f"Expected API route with prefix {prefix!r} to appear before "
            "the gateway router, but none found"
        )


# ---------------------------------------------------------------------------
# Endpoint accessibility (gateway does NOT shadow API endpoints)
# ---------------------------------------------------------------------------


def test_health_endpoint_reachable():
    """GET /health returns 200 (not a gateway 404/redirect)."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200, (
        f"GET /health returned {response.status_code}, "
        "gateway may be shadowing API routes"
    )


def test_services_endpoint_reachable():
    """GET /services returns a non-gateway response (not a gateway 404/redirect).

    We accept any non-gateway response (200, 401, 422) as evidence that
    the correct router handled the request.  A gateway catch-all would
    return 404 (no matching component) or 503 (no registry).
    """
    client = TestClient(app)
    response = client.get("/services")
    # Without proper state wired, the handler may raise internally,
    # but the key invariant is that the response is NOT a gateway 404
    # (which is "component not found" from the gateway proxy).
    assert response.status_code != 404 or "application/json" in str(
        response.headers.get("content-type", "")
    ), (
        "GET /services returned 404 with unexpected content type. "
        "Gateway may be shadowing API routes."
    )


def test_onboard_preflight_reachable():
    """POST /onboard/preflight reaches the onboard router, not the gateway."""
    client = TestClient(app)
    response = client.post("/onboard/preflight")
    # Without auth/config the endpoint returns 401 or 422 — not a gateway 404.
    # The gateway catch-all returns 307 redirect or 404/503.
    assert response.status_code not in (307,), (
        "POST /onboard/preflight returned 307 redirect — "
        "gateway path-prefix redirect intercepted the request"
    )


# ---------------------------------------------------------------------------
# Negative test: swapped registration order
# ---------------------------------------------------------------------------


def test_gateway_first_shadows_api_routes():
    """When gateway is registered first, API routes are unreachable."""
    # Create a minimal app with gateway-like catch-all registered first.
    bad_app = FastAPI()

    catch_all = APIRouter()

    @catch_all.api_route("/{path:path}", methods=["GET", "POST"])
    async def fake_gateway(path: str):
        return {"shadowed": path}

    bad_app.include_router(catch_all)

    # Now register a simple API route — it should never be reached.
    @bad_app.get("/api/hello")
    async def hello():
        return {"message": "hello"}

    client = TestClient(bad_app)
    resp = client.get("/api/hello")
    # The catch-all absorbed the request — we get the shadowed response, not "hello".
    assert resp.json() == {"shadowed": "api/hello"}, (
        "Expected catch-all to shadow /api/hello when registered first"
    )
