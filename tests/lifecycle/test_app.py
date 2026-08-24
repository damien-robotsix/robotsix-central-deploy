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


# ---------------------------------------------------------------------------
# No component-level auth dependency — fleet edge is the only gate
# ---------------------------------------------------------------------------


def test_no_route_has_real_auth_dependency():
    """No route may carry a real (non-no-op) auth dependency.

    Component-level authentication was removed (auth-removal epic):
    the fleet edge (Traefik + tinyauth) is the only gate.  The
    ``verify_auth`` function is a no-op stub that must never be
    replaced with a real credential check, and no new auth-like
    dependency may be added to any route.

    The ForwardAuth endpoint (``/auth/validate``) is the one exception:
    it calls ``verify_bearer_token`` **inline** (not as a FastAPI
    dependency), so it does not appear in the dependency tree.
    """
    import asyncio
    from unittest.mock import MagicMock

    from fastapi.routing import APIRoute

    from robotsix_central_deploy.lifecycle.auth import verify_auth

    # 1. verify_auth must be a no-op — call it and confirm it returns
    #    None without raising.
    mock_request = MagicMock()
    result = asyncio.run(verify_auth(mock_request))
    assert result is None, f"verify_auth must be a no-op returning None, got {result!r}"

    # 2. Walk every route and check its dependency tree for auth-like
    #    dependencies that are not the known verify_auth no-op stub.
    #    An auth dependency is one that guards access — the function
    #    name starts with ``verify`` or ``check`` and mentions auth,
    #    or the function lives in the ``lifecycle.auth`` module.
    auth_module_names = ("robotsix_central_deploy.lifecycle.auth",)

    for route in _flatten_routes(app):
        if not isinstance(route, APIRoute):
            continue

        # Check route-level dependencies (from decorator).
        for dep in getattr(route, "dependencies", ()):
            _check_dep_callable(route.path, dep.dependency, auth_module_names)

        # Check the full resolved dependant tree (signature-level Depends).
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            _check_dependant_tree(route.path, dependant, auth_module_names)


def _check_dep_callable(
    route_path: str,
    callable_obj: object | None,
    auth_module_names: tuple[str, ...],
) -> None:
    """Fail if *callable_obj* is an auth function that is not the no-op stub.

    An auth function is one whose module is in *auth_module_names* or whose
    name starts with ``verify_`` / ``check_`` and mentions ``auth``.
    """
    if callable_obj is None:
        return

    from robotsix_central_deploy.lifecycle.auth import verify_auth

    if callable_obj is verify_auth:
        return  # Known no-op stub — permitted.

    mod = getattr(callable_obj, "__module__", "")
    name = getattr(callable_obj, "__name__", "")

    # Match functions defined in the auth module.
    if mod in auth_module_names:
        raise AssertionError(
            f"Route {route_path!r} carries an auth-module dependency "
            f"{name!r} (from {mod}) that is not the verify_auth no-op stub."
        )

    # Match functions whose name pattern suggests a credential guard.
    if name.startswith(("verify_", "check_")) and "auth" in name.lower():
        raise AssertionError(
            f"Route {route_path!r} carries an auth-guard dependency "
            f"{name!r} that is not the verify_auth no-op stub."
        )


def _check_dependant_tree(
    route_path: str,
    dependant: object,
    auth_module_names: tuple[str, ...],
) -> None:
    """Recursively inspect a Dependant tree for real auth dependencies."""
    # NOTE: dependant.call at the root is the *endpoint function* itself,
    # not a dependency — only recurse into the dependencies list.
    for sub in getattr(dependant, "dependencies", []):
        _check_dep_callable(route_path, getattr(sub, "call", None), auth_module_names)
        _check_dependant_tree(route_path, sub, auth_module_names)
