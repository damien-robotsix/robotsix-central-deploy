"""Tests for Traefik label derivation."""

from __future__ import annotations

import pytest

from robotsix_central_deploy.registry.constants import PROXY_NETWORK
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping
from robotsix_central_deploy.registry.traefik_labels import (
    BROWSER_MIDDLEWARE,
    traefik_labels,
)

BASE = "deploy.robotsix.net"


def _config(**overrides: object) -> ComponentConfig:
    defaults: dict[str, object] = {
        "id": "board",
        "image": "ghcr.io/org/board:main",
        "container_name": "robotsix-board",
        "ports": [PortMapping(host=0, container=8080)],
    }
    defaults.update(overrides)
    return ComponentConfig(**defaults)  # type: ignore[arg-type]


def _labels(**overrides: object) -> dict[str, str]:
    return traefik_labels(_config(**overrides), BASE, PROXY_NETWORK)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_routes_component_at_its_subdomain() -> None:
    labels = _labels()
    assert labels["traefik.enable"] == "true"
    assert (
        labels["traefik.http.routers.board.rule"] == "Host(`board.deploy.robotsix.net`)"
    )


def test_forwards_to_the_container_port_not_the_host_port() -> None:
    # Host ports are never published; Traefik dials the container port over
    # the shared network.
    labels = _labels(ports=[PortMapping(host=9999, container=8080)])
    assert labels["traefik.http.services.board.loadbalancer.server.port"] == "8080"
    assert labels["traefik.docker.network"] == PROXY_NETWORK


def test_both_routers_share_one_upstream_service() -> None:
    # Without an explicit service, Traefik invents one per router and 404s the
    # one that has no matching service definition.
    labels = _labels()
    for router in ("board", "board-health"):
        assert labels[f"traefik.http.routers.{router}.service"] == "board"


# ---------------------------------------------------------------------------
# Authentication — the point of the redesign
# ---------------------------------------------------------------------------


def test_browser_traffic_goes_through_sso() -> None:
    assert _labels()["traefik.http.routers.board.middlewares"] == BROWSER_MIDDLEWARE


def test_no_router_bypasses_sso_except_health() -> None:
    """Only /health may answer without the SSO gate.

    A second, weaker door existed here: an HTTP Basic router matched on the
    Authorization header. Keyed with the fleet's existing htpasswd, it meant
    every browser holding the old credential was served without ever seeing a
    login page. Nothing may reintroduce a route that skips tinyauth.
    """
    labels = _labels()
    routers = {k.split(".")[3] for k in labels if k.startswith("traefik.http.routers.")}
    assert routers == {"board", "board-health"}
    assert labels["traefik.http.routers.board.middlewares"] == BROWSER_MIDDLEWARE
    assert not any("Authorization" in v for v in labels.values())


def test_health_probe_is_auth_exempt() -> None:
    # The fleet health-endpoint standard requires an uncredentialed probe.
    labels = _labels()
    assert "Path(`/health`)" in labels["traefik.http.routers.board-health.rule"]
    assert "traefik.http.routers.board-health.middlewares" not in labels


def test_health_router_outranks_the_catch_all() -> None:
    labels = _labels()
    priority = lambda r: int(labels[f"traefik.http.routers.{r}.priority"])
    assert priority("board-health") > priority("board")


# ---------------------------------------------------------------------------
# Not routable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"routable": False}, "siblings must stay on the internal network"),
        ({"ports": []}, "a component with no HTTP surface has nothing to route"),
    ],
)
def test_unroutable_components_get_no_labels(
    overrides: dict[str, object], reason: str
) -> None:
    assert _labels(**overrides) == {}, reason


def test_no_labels_until_a_base_domain_is_configured() -> None:
    assert traefik_labels(_config(), "", PROXY_NETWORK) == {}


# ---------------------------------------------------------------------------
# Repo-agnostic rule
# ---------------------------------------------------------------------------


def test_labels_are_derived_never_special_cased() -> None:
    # central-deploy is a generic engine: an arbitrary new component id must
    # produce a complete label set with no entry in any allowlist.
    labels = traefik_labels(
        _config(
            id="brand-new-thing",
            container_name="x",
            ports=[PortMapping(host=0, container=3000)],
        ),
        BASE,
        PROXY_NETWORK,
    )
    assert (
        labels["traefik.http.routers.brand-new-thing.rule"]
        == "Host(`brand-new-thing.deploy.robotsix.net`)"
    )
    assert (
        labels["traefik.http.services.brand-new-thing.loadbalancer.server.port"]
        == "3000"
    )
