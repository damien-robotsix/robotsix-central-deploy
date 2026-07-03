"""Tests for the FastAPI app router registration ordering.

The gateway router MUST be registered last — it includes catch-all routes
(``/{path:path}``) that would shadow specific API routes if registered earlier.
"""

from __future__ import annotations

from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient
from starlette.routing import Match

from robotsix_central_deploy.lifecycle.app import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_gateway_idx(app: FastAPI) -> int | None:
    """Return the index of the first gateway-tagged route, or None."""
    for i, route in enumerate(app.router.routes):
        if hasattr(route, "tags") and "gateway" in (route.tags or []):
            return i
    return None


def _first_matching_route(app: FastAPI, method: str, path: str) -> object | None:
    """Return the first route that matches *method* *path*, or None."""
    scope: dict = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match != Match.NONE:
            return route
    return None


# ---------------------------------------------------------------------------
# Route ordering invariant
# ---------------------------------------------------------------------------


def test_gateway_router_registered_last():
    """Gateway catch-all routes appear after every non-gateway route."""
    first = _first_gateway_idx(app)
    assert first is not None, "gateway router not found in app routes"

    routes = list(app.router.routes)

    # Every route after the first gateway route must also be a gateway route
    # (i.e., no non-gateway route is registered after the gateway).
    for i in range(first + 1, len(routes)):
        route = routes[i]
        tags = getattr(route, "tags", []) or []
        path = route.path

        # A route is considered "gateway" if it has the gateway tag, OR if
        # it's a known gateway catch-all path (e.g. the WebSocket route,
        # which as APIWebSocketRoute does not inherit the router's tags).
        is_gateway = ("gateway" in tags) or (path in ("/{path:path}", "/"))

        assert is_gateway, (
            f"Non-gateway route at position {i} (path={path!r}) "
            f"appears after the first gateway route at position {first}"
        )


def test_specific_api_routes_before_gateway():
    """Every well-known API route is registered before the first gateway route."""
    first = _first_gateway_idx(app)
    assert first is not None, "gateway router not found in app routes"

    routes = list(app.router.routes)

    # Collect the paths of all routes before the gateway.
    api_paths: set[str] = set()
    for route in routes[:first]:
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
    """The first route matching GET /services is NOT a gateway route.

    Uses Starlette route matching rather than TestClient because the
    services router and the gateway catch-all both require app state
    (lifespan) to function — a bare TestClient cannot distinguish them
    via HTTP responses alone.
    """
    route = _first_matching_route(app, "GET", "/services")
    assert route is not None, "No route matches GET /services"
    tags = getattr(route, "tags", []) or []
    assert "gateway" not in tags, (
        "GET /services matched a gateway route — gateway is shadowing API routes"
    )


def test_onboard_preflight_reachable():
    """The first route matching POST /onboard/preflight is NOT a gateway route.

    Same rationale as test_services_endpoint_reachable: both the onboard
    router and the gateway require app state, so we verify routing
    structurally rather than via HTTP.
    """
    route = _first_matching_route(app, "POST", "/onboard/preflight")
    assert route is not None, "No route matches POST /onboard/preflight"
    tags = getattr(route, "tags", []) or []
    assert "gateway" not in tags, (
        "POST /onboard/preflight matched a gateway route — "
        "gateway is shadowing API routes"
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
