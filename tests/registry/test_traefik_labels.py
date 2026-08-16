"""Tests for Traefik label derivation."""

from __future__ import annotations

import pytest

from robotsix_central_deploy.registry.constants import PROXY_NETWORK
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping
from robotsix_central_deploy.registry.traefik_labels import (
    BEARER_MIDDLEWARE,
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


def test_all_routers_share_one_upstream_service() -> None:
    # Without an explicit service, Traefik invents one per router and 404s the
    # one that has no matching service definition.
    labels = _labels()
    for router in ("board", "board-bearer", "board-health"):
        assert labels[f"traefik.http.routers.{router}.service"] == "board"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_browser_traffic_goes_through_sso() -> None:
    assert _labels()["traefik.http.routers.board.middlewares"] == BROWSER_MIDDLEWARE


def test_bearer_traffic_uses_mobile_token_auth() -> None:
    """Requests with Authorization: Bearer use the mobile-token ForwardAuth."""
    labels = _labels()
    assert labels["traefik.http.routers.board-bearer.middlewares"] == BEARER_MIDDLEWARE
    assert (
        "HeaderRegexp(`Authorization`, `^Bearer .+`)"
        in labels["traefik.http.routers.board-bearer.rule"]
    )


def test_three_routers_per_component() -> None:
    """Every routable component gets health, bearer, and browser routers.

    The bearer router (priority 20) sits between health (30) and browser
    (10), so bearer-token requests skip tinyauth entirely while browser
    sessions fall through to the SSO gate.
    """
    labels = _labels()
    routers = {k.split(".")[3] for k in labels if k.startswith("traefik.http.routers.")}
    assert routers == {"board", "board-bearer", "board-health"}


def test_health_probe_is_auth_exempt() -> None:
    # The fleet health-endpoint standard requires an uncredentialed probe.
    labels = _labels()
    assert "Path(`/health`)" in labels["traefik.http.routers.board-health.rule"]
    assert "traefik.http.routers.board-health.middlewares" not in labels


def test_router_priority_order() -> None:
    """Health (30) > bearer (20) > browser (10)."""
    labels = _labels()
    priority = lambda r: int(labels[f"traefik.http.routers.{r}.priority"])
    assert priority("board-health") > priority("board-bearer") > priority("board")


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
    # Bearer router exists and has the expected rule.
    assert (
        "HeaderRegexp(`Authorization`, `^Bearer .+`)"
        in labels["traefik.http.routers.brand-new-thing-bearer.rule"]
    )


class TestPublicUrl:
    """The URL reported for a component must match what the edge actually serves."""

    def _config(self, **overrides):
        from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping

        fields = {
            "id": "widget",
            "image": "widget:latest",
            "container_name": "widget",
            "ports": [PortMapping(host=8300, container=8080, protocol="tcp")],
        }
        fields.update(overrides)
        return ComponentConfig(**fields)

    def test_routed_component_reports_its_url(self):
        from robotsix_central_deploy.registry.traefik_labels import public_url

        assert (
            public_url(self._config(), "deploy.robotsix.net")
            == "https://widget.deploy.robotsix.net"
        )

    def test_component_without_a_port_has_no_url(self):
        """Regression: the case that 404'd.

        A component with no port gets no Traefik labels, so reporting a public
        URL for it hands out a link that cannot work — which is precisely how
        an unrouted component passed for healthy.
        """
        from robotsix_central_deploy.registry.traefik_labels import public_url

        assert public_url(self._config(ports=[]), "deploy.robotsix.net") is None

    def test_non_routable_component_has_no_url(self):
        from robotsix_central_deploy.registry.traefik_labels import public_url

        assert public_url(self._config(routable=False), "deploy.robotsix.net") is None

    def test_no_gateway_configured_means_no_url(self):
        from robotsix_central_deploy.registry.traefik_labels import public_url

        assert public_url(self._config(), "") is None

    def test_url_is_reported_exactly_when_labels_are_emitted(self):
        """The predicate behind the URL and the predicate behind the route are one."""
        from robotsix_central_deploy.registry.traefik_labels import (
            public_url,
            traefik_labels,
        )

        for cfg, domain in (
            (self._config(), "deploy.robotsix.net"),
            (self._config(ports=[]), "deploy.robotsix.net"),
            (self._config(routable=False), "deploy.robotsix.net"),
            (self._config(), ""),
        ):
            has_labels = bool(traefik_labels(cfg, domain, "central-deploy-proxy"))
            has_url = public_url(cfg, domain) is not None
            assert has_labels == has_url
