"""Unit tests for deploy router helpers."""

from __future__ import annotations

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
