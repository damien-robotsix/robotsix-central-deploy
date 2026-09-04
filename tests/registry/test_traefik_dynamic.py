"""Tests for Traefik file-provider fragment rendering (remote-host components)."""

from __future__ import annotations

from pathlib import Path

import yaml

from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping
from robotsix_central_deploy.registry.traefik_dynamic import (
    fragment_path,
    remove_fragment,
    render_remote_dynamic_config,
    write_fragment,
)
from robotsix_central_deploy.registry.traefik_labels import (
    BEARER_MIDDLEWARE,
    BROWSER_MIDDLEWARE,
)

BASE = "deploy.robotsix.net"
REACH = "10.88.0.2"


def _config(**overrides: object) -> ComponentConfig:
    defaults: dict[str, object] = {
        "id": "hexarchy",
        "image": "ghcr.io/org/hexarchy:main",
        "container_name": "hexarchy",
        "host": "bequiet",
        # The edge dials the HOST side of the mapping — the port published
        # on the remote host's tunnel address.
        "ports": [PortMapping(host=8000, container=8000)],
    }
    defaults.update(overrides)
    return ComponentConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Rendering — mirrors traefik_labels router-for-router
# ---------------------------------------------------------------------------


def test_renders_three_routers_and_reach_url() -> None:
    rendered = render_remote_dynamic_config(_config(), BASE, REACH)
    routers = rendered["http"]["routers"]
    assert set(routers) == {"hexarchy", "hexarchy-bearer", "hexarchy-health"}
    assert routers["hexarchy"]["rule"] == "Host(`hexarchy.deploy.robotsix.net`)"
    assert routers["hexarchy"]["middlewares"] == [BROWSER_MIDDLEWARE]
    assert routers["hexarchy-bearer"]["middlewares"] == [BEARER_MIDDLEWARE]
    assert "middlewares" not in routers["hexarchy-health"]
    # Priorities match the label-based routing so behaviour is identical.
    assert routers["hexarchy-health"]["priority"] == 30
    assert routers["hexarchy-bearer"]["priority"] == 20
    assert routers["hexarchy"]["priority"] == 10
    servers = rendered["http"]["services"]["hexarchy"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://10.88.0.2:8000"}]


def test_dials_the_host_port_not_the_container_port() -> None:
    rendered = render_remote_dynamic_config(
        _config(ports=[PortMapping(host=9999, container=8080)]), BASE, REACH
    )
    servers = rendered["http"]["services"]["hexarchy"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://10.88.0.2:9999"}]


def test_every_router_targets_the_single_service() -> None:
    rendered = render_remote_dynamic_config(_config(), BASE, REACH)
    for entry in rendered["http"]["routers"].values():
        assert entry["service"] == "hexarchy"
        assert entry["entryPoints"] == ["websecure"]


def test_unroutable_states_render_nothing() -> None:
    assert render_remote_dynamic_config(_config(), "", REACH) == {}
    assert render_remote_dynamic_config(_config(ports=[]), BASE, REACH) == {}
    assert render_remote_dynamic_config(_config(routable=False), BASE, REACH) == {}
    assert render_remote_dynamic_config(_config(), BASE, "") == {}


# ---------------------------------------------------------------------------
# Fragment file lifecycle
# ---------------------------------------------------------------------------


def test_write_fragment_produces_parseable_yaml(tmp_path: Path) -> None:
    path = write_fragment(str(tmp_path), _config(), BASE, REACH)
    assert path == tmp_path / "remote-hexarchy.yml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == render_remote_dynamic_config(_config(), BASE, REACH)
    # No half-written temp file left behind (atomic replace).
    assert not list(tmp_path.glob("*.tmp"))


def test_write_fragment_removes_stale_file_when_unroutable(tmp_path: Path) -> None:
    write_fragment(str(tmp_path), _config(), BASE, REACH)
    assert write_fragment(str(tmp_path), _config(routable=False), BASE, REACH) is None
    assert not fragment_path(str(tmp_path), "hexarchy").exists()


def test_remove_fragment_is_idempotent(tmp_path: Path) -> None:
    write_fragment(str(tmp_path), _config(), BASE, REACH)
    remove_fragment(str(tmp_path), "hexarchy")
    assert not fragment_path(str(tmp_path), "hexarchy").exists()
    remove_fragment(str(tmp_path), "hexarchy")  # absent → no raise
