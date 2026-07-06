"""Tests for onboard port_utils — host-port collision helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from robotsix_central_deploy.onboard.port_utils import (
    collect_occupied_host_ports,
    find_free_host_port,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    ServiceConfig,
)


def _store(*configs: ComponentConfig) -> ComponentConfigStore:
    """Create a temporary-file-backed store pre-loaded with the given configs."""
    store = ComponentConfigStore(Path(tempfile.mkstemp(suffix=".json")[1]))
    for cfg in configs:
        store.register(cfg)
    return store


def _cfg(
    id: str,
    host_ports: tuple[int, ...] = (),
    sibling_host_ports: dict[str, tuple[int, ...]] | None = None,
) -> ComponentConfig:
    """Minimal ComponentConfig with host ports spread across primary + optional siblings."""
    cfg = ComponentConfig(
        id=id,
        image="img:latest",
        container_name=id,
        ports=[PortMapping(host=p, container=p) for p in host_ports],
    )
    if sibling_host_ports:
        cfg.siblings = [
            ServiceConfig(
                service_key=key,
                container_name=f"{id}-{key}",
                image="img:latest",
                ports=[PortMapping(host=p, container=p) for p in ports],
            )
            for key, ports in sibling_host_ports.items()
        ]
    return cfg


# ---------------------------------------------------------------------------
# collect_occupied_host_ports
# ---------------------------------------------------------------------------


class TestCollectOccupiedHostPorts:
    def test_empty_store_returns_only_lifecycle_port(self):
        store = _store()
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100}

    def test_single_component_with_no_ports(self):
        store = _store(_cfg("app"))
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100}

    def test_single_component_ports_plus_lifecycle(self):
        store = _store(_cfg("app", host_ports=(8080, 8443)))
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8080, 8443}

    def test_multiple_components_non_overlapping(self):
        store = _store(
            _cfg("a", host_ports=(8001,)),
            _cfg("b", host_ports=(8002, 8003)),
        )
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8001, 8002, 8003}

    def test_overlapping_ports_across_components_collapse_to_set(self):
        store = _store(
            _cfg("a", host_ports=(8001, 8002)),
            _cfg("b", host_ports=(8002, 8003)),
        )
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8001, 8002, 8003}

    def test_sibling_ports_included(self):
        store = _store(
            _cfg("app", host_ports=(8080,), sibling_host_ports={"worker": (9090,)}),
        )
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8080, 9090}

    def test_multiple_siblings_all_ports_collected(self):
        store = _store(
            _cfg(
                "app",
                host_ports=(8080,),
                sibling_host_ports={"worker": (9090,), "db": (5432,)},
            ),
        )
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8080, 9090, 5432}

    def test_lifecycle_port_in_occupied_set_included(self):
        """When lifecycle port overlaps with a component, it's still in the set."""
        store = _store(_cfg("app", host_ports=(8100, 8080)))
        result = collect_occupied_host_ports(store, lifecycle_port=8100)
        assert result == {8100, 8080}


# ---------------------------------------------------------------------------
# find_free_host_port
# ---------------------------------------------------------------------------


class TestFindFreeHostPort:
    def test_no_occupied_returns_start(self):
        assert find_free_host_port(set(), start=10000, end=20000) == 10000

    def test_start_occupied_returns_next(self):
        assert find_free_host_port({10000}, start=10000, end=20000) == 10001

    def test_consecutive_occupied_skips_them(self):
        assert (
            find_free_host_port({10000, 10001, 10002}, start=10000, end=20000) == 10003
        )

    def test_gap_in_middle_finds_first_free(self):
        assert find_free_host_port({10000, 10002}, start=10000, end=20000) == 10001

    def test_all_ports_occupied_raises_runtime_error(self):
        occupied = set(range(10000, 10010))
        with pytest.raises(RuntimeError) as exc:
            find_free_host_port(occupied, start=10000, end=10010)
        assert "No free host port" in str(exc.value)
        assert "[10000, 10010)" in str(exc.value)

    def test_free_at_end_boundary(self):
        occupied = set(range(10000, 19999))
        assert find_free_host_port(occupied, start=10000, end=20000) == 19999

    def test_large_range_with_random_occupied(self):
        occupied = {10000, 10001, 10002, 10003, 10004}
        assert find_free_host_port(occupied, start=10000, end=20000) == 10005

    def test_custom_range(self):
        occupied = {5000, 5001}
        assert find_free_host_port(occupied, start=5000, end=5010) == 5002
