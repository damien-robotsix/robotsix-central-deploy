"""Tests for the FastAPI app's route table.

This app used to end with a catch-all gateway router that proxied component
traffic, which forced a fragile "must be registered last" ordering invariant
and a reserved-name list. Component traffic is now carried by the Traefik edge
and never enters this app, so the invariant is stronger and simpler: there is
no catch-all route at all.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Match, Mount

from robotsix_central_deploy.lifecycle.app import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_routes(app: FastAPI) -> list:
    """Return all leaf routes in registration order.

    Recurses into ``_IncludedRouter`` (FastAPI ≥ 0.139) and ``Mount``
    containers so that the tests work regardless of the FastAPI version.
    """

    def _collect(route: object, dest: list) -> None:
        # FastAPI ≥ 0.139: _IncludedRouter wraps an APIRouter
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                _collect(sub, dest)
            return
        # Starlette Mount / Host
        if isinstance(route, Mount):
            for sub in route.routes:
                _collect(sub, dest)
            return
        dest.append(route)

    flat: list = []
    for route in app.router.routes:
        _collect(route, flat)
    return flat


def _first_matching_route(app: FastAPI, method: str, path: str) -> object | None:
    """Return the first route that matches *method* *path*, or None."""
    scope: dict = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    for route in _flatten_routes(app):
        match, _ = route.matches(scope)
        if match != Match.NONE:
            return route
    return None


# ---------------------------------------------------------------------------
# No catch-all
# ---------------------------------------------------------------------------


def test_app_has_no_catch_all_route():
    """No route may swallow arbitrary paths.

    A ``/{path:path}`` route shadows every API endpoint registered after it,
    which is exactly what made the old in-app gateway need its ordering rule.
    Re-introducing one would bring the whole class of bug back.
    """
    offenders = [
        route.path
        for route in _flatten_routes(app)
        if "{path:path}" in getattr(route, "path", "")
        # The dashboard's static-file route is scoped under /ui/static and
        # resolves within a fixed root, so it shadows nothing.
        and not getattr(route, "path", "").startswith("/ui/static")
    ]
    assert offenders == [], f"catch-all route(s) registered: {offenders}"


def test_no_gateway_module_remains():
    """The proxy package is gone, not merely unregistered."""
    import importlib

    for module in (
        "robotsix_central_deploy.gateway.router",
        "robotsix_central_deploy.gateway.proxy",
        "robotsix_central_deploy.lifecycle.gateway_docs_middleware",
        "robotsix_central_deploy.lifecycle.session",
    ):
        try:
            importlib.import_module(module)
        except ImportError:
            continue
        raise AssertionError(f"{module} should have been removed")


# ---------------------------------------------------------------------------
# Endpoint accessibility
# ---------------------------------------------------------------------------


def test_health_endpoint_reachable():
    """GET /health returns 200."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200


def test_key_api_routes_are_registered():
    """The well-known API surface is present."""
    paths = {getattr(r, "path", "") for r in _flatten_routes(app)}
    for prefix in (
        "/health",
        "/services",
        "/system/update",
        "/onboard/preflight",
        "/ui",
    ):
        assert any(p.startswith(prefix) for p in paths), f"missing route {prefix!r}"


def test_services_route_matches_the_services_router():
    route = _first_matching_route(app, "GET", "/services")
    assert route is not None, "No route matches GET /services"
    endpoint = getattr(route, "endpoint", None)
    module = getattr(endpoint, "__module__", "")
    assert module.startswith("robotsix_central_deploy.lifecycle.routers"), module


def test_onboard_preflight_route_matches_the_onboard_router():
    route = _first_matching_route(app, "POST", "/onboard/preflight")
    assert route is not None, "No route matches POST /onboard/preflight"
    endpoint = getattr(route, "endpoint", None)
    module = getattr(endpoint, "__module__", "")
    assert module.startswith("robotsix_central_deploy.lifecycle.routers"), module
