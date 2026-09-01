"""Unit tests for deploy router helpers."""

from __future__ import annotations

from typing import Self

import pytest

from robotsix_central_deploy.lifecycle.routers.services_deploy import (
    _build_sibling_config,
)
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    HealthCheck,
    PortMapping,
    ServiceConfig,
    VolumeMount,
)

# ---------------------------------------------------------------------------
# _build_sibling_config
# ---------------------------------------------------------------------------


def test_build_sibling_config_full_mapping() -> None:
    """All ServiceConfig fields map correctly into the ComponentConfig."""
    sib = ServiceConfig(
        service_key="redis",
        image="redis:7-alpine",
        container_name="myapp-redis",
        ports=[PortMapping(host=6379, container=6379, protocol="tcp")],
        mounts=[VolumeMount(host="redis_data", container="/data", read_only=False)],
        env={"REDIS_PASSWORD": "secret"},
        health_check=HealthCheck(
            test=["CMD", "redis-cli", "ping"],
            interval=10_000_000_000,
            timeout=2_000_000_000,
            retries=3,
        ),
        claude_mount=False,
        host_docker_sock=False,
        command=["redis-server", "--appendonly", "yes"],
        entrypoint=["/usr/local/bin/docker-entrypoint.sh"],
        tmpfs=["/tmp"],
        mem_limit="256m",
        user="redis",
    )
    merged_env = {"EXTRA": "value"}

    result = _build_sibling_config(sib, sib_name="myapp-redis", merged_env=merged_env)

    assert isinstance(result, ComponentConfig)
    assert result.id == "myapp-redis"
    assert result.image == "redis:7-alpine"
    assert result.container_name == "myapp-redis"
    assert result.ports == [PortMapping(host=6379, container=6379, protocol="tcp")]
    assert result.mounts == [
        VolumeMount(host="redis_data", container="/data", read_only=False)
    ]
    assert result.health_check == HealthCheck(
        test=["CMD", "redis-cli", "ping"],
        interval=10_000_000_000,
        timeout=2_000_000_000,
        retries=3,
    )
    assert result.claude_mount is False
    assert result.host_docker_sock is False
    assert result.command == ["redis-server", "--appendonly", "yes"]
    assert result.entrypoint == ["/usr/local/bin/docker-entrypoint.sh"]
    assert result.tmpfs == ["/tmp"]
    assert result.mem_limit == "256m"
    assert result.user == "redis"
    # env is merged_env, not sib_config.env
    assert result.env == {"EXTRA": "value"}
    # named_volumes derived from mount hosts
    assert result.named_volumes == ["redis_data"]


def test_build_sibling_config_env_uses_merged_not_sib_env() -> None:
    """merged_env overrides sib_config.env — the helper never reads sib_config.env."""
    sib = ServiceConfig(
        service_key="svc",
        image="alpine:latest",
        container_name="svc-alpine",
        env={"IGNORED": "yes"},
    )
    result = _build_sibling_config(sib, sib_name="x", merged_env={"REAL": "val"})
    assert result.env == {"REAL": "val"}


def test_build_sibling_config_named_volumes_from_mounts() -> None:
    """named_volumes is [m.host for m in mounts]."""
    sib = ServiceConfig(
        service_key="svc",
        image="alpine:latest",
        container_name="svc-alpine",
        mounts=[
            VolumeMount(host="vol_a", container="/a", read_only=False),
            VolumeMount(host="vol_b", container="/b", read_only=True),
        ],
    )
    result = _build_sibling_config(sib, sib_name="x", merged_env={})
    assert result.named_volumes == ["vol_a", "vol_b"]


def test_build_sibling_config_defaults_preserved() -> None:
    """Fields not set on ServiceConfig get their pydantic defaults."""
    sib = ServiceConfig(
        service_key="svc",
        image="alpine:latest",
        container_name="svc-alpine",
    )
    result = _build_sibling_config(sib, sib_name="default-sib", merged_env={})
    assert result.id == "default-sib"
    assert result.image == "alpine:latest"
    assert result.container_name == "svc-alpine"
    assert result.ports == []
    assert result.mounts == []
    assert result.health_check is None
    assert result.claude_mount is False
    assert result.host_docker_sock is False
    assert result.command is None
    assert result.entrypoint is None
    assert result.tmpfs == []
    assert result.mem_limit == "2g"  # ComponentConfig default
    assert result.user is None  # ComponentConfig default
    assert result.env == {}
    assert result.named_volumes == []


# ---------------------------------------------------------------------------
# _verify_edge_route
# ---------------------------------------------------------------------------


def _routable_config() -> ComponentConfig:
    return ComponentConfig(
        id="widget",
        image="widget:latest",
        container_name="widget",
        ports=[PortMapping(host=8300, container=8080, protocol="tcp")],
    )


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient, replaying a scripted set of outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.requested: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        self.requested.append(url)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, _FakeResponse)
        return outcome


def _patch_probe(monkeypatch, outcomes: list[object]) -> _FakeClient:
    """Install the fake client and remove the inter-attempt sleep."""
    import robotsix_central_deploy.lifecycle.routers.services_deploy as mod

    client = _FakeClient(outcomes)
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: client)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    return client


@pytest.mark.asyncio
async def test_verify_edge_route_passes_when_the_edge_answers(monkeypatch) -> None:
    """A 200 from the auth-exempt /health router means the route exists."""
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _verify_edge_route,
    )

    client = _patch_probe(monkeypatch, [_FakeResponse(200)])

    assert await _verify_edge_route(_routable_config(), "deploy.robotsix.net") is None
    assert client.requested == ["https://widget.deploy.robotsix.net/health"]


@pytest.mark.asyncio
async def test_verify_edge_route_accepts_the_sso_gate(monkeypatch) -> None:
    """401 means the edge routed the request and tinyauth answered — healthy.

    docs/edge.md is explicit that 401 is a healthy answer and only 404 means
    no router exists; treating the gate's own reply as a failure would warn on
    every correctly-routed component.
    """
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _verify_edge_route,
    )

    _patch_probe(monkeypatch, [_FakeResponse(401)])

    assert await _verify_edge_route(_routable_config(), "deploy.robotsix.net") is None


@pytest.mark.asyncio
async def test_verify_edge_route_warns_when_no_router_exists(monkeypatch) -> None:
    """The regression: healthy container, 404 at the public URL.

    This is the signal that was missing entirely — file-hub deployed healthy
    for days behind a hostname the edge had no router for.
    """
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _EDGE_PROBE_ATTEMPTS,
        _verify_edge_route,
    )

    client = _patch_probe(
        monkeypatch, [_FakeResponse(404) for _ in range(_EDGE_PROBE_ATTEMPTS)]
    )

    warning = await _verify_edge_route(_routable_config(), "deploy.robotsix.net")

    assert warning is not None
    assert "404" in warning
    assert "https://widget.deploy.robotsix.net/health" in warning
    # It retried rather than judging on the recreate window alone.
    assert len(client.requested) == _EDGE_PROBE_ATTEMPTS


@pytest.mark.asyncio
async def test_verify_edge_route_tolerates_the_recreate_window(monkeypatch) -> None:
    """A 404 while Traefik is still catching up must not raise a false alarm.

    Recreating a container takes its route with it for a few seconds, so the
    first probe of a perfectly healthy component can legitimately 404.
    """
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _verify_edge_route,
    )

    _patch_probe(
        monkeypatch, [_FakeResponse(404), _FakeResponse(404), _FakeResponse(200)]
    )

    assert await _verify_edge_route(_routable_config(), "deploy.robotsix.net") is None


@pytest.mark.asyncio
async def test_verify_edge_route_reports_a_bad_gateway(monkeypatch) -> None:
    """A route that exists but leads nowhere is worth saying out loud."""
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _EDGE_PROBE_ATTEMPTS,
        _verify_edge_route,
    )

    _patch_probe(monkeypatch, [_FakeResponse(502) for _ in range(_EDGE_PROBE_ATTEMPTS)])

    warning = await _verify_edge_route(_routable_config(), "deploy.robotsix.net")

    assert warning is not None
    assert "502" in warning


@pytest.mark.asyncio
async def test_verify_edge_route_skips_unroutable_components(monkeypatch) -> None:
    """No gateway, no port, or not routable: there is no URL to hold to account."""
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _verify_edge_route,
    )

    client = _patch_probe(monkeypatch, [])

    assert await _verify_edge_route(_routable_config(), "") is None
    assert (
        await _verify_edge_route(
            _routable_config().model_copy(update={"ports": []}),
            "deploy.robotsix.net",
        )
        is None
    )
    assert client.requested == []


@pytest.mark.asyncio
async def test_verify_edge_route_never_raises(monkeypatch) -> None:
    """A transport failure becomes a warning, never an exception.

    The check is advisory: it must not be able to fail a deploy that
    otherwise succeeded.
    """
    from robotsix_central_deploy.lifecycle.routers.services_deploy import (
        _EDGE_PROBE_ATTEMPTS,
        _verify_edge_route,
    )

    _patch_probe(
        monkeypatch,
        [ConnectionError("dns is down") for _ in range(_EDGE_PROBE_ATTEMPTS)],
    )

    warning = await _verify_edge_route(_routable_config(), "deploy.robotsix.net")

    assert warning is not None
    assert "ConnectionError" in warning
