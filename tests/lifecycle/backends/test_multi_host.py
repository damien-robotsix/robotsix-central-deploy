"""Tests for the multi-host routing facade and remote-mode container create."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backends.base import ExecutionBackend
from robotsix_central_deploy.lifecycle.backends.docker_sdk import DockerSdkBackend
from robotsix_central_deploy.lifecycle.backends.multi_host import MultiHostBackend
from robotsix_central_deploy.lifecycle.config import RemoteHostEntry
from robotsix_central_deploy.lifecycle.models import ServiceRecord
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    VolumeMount,
)

BASE = "deploy.robotsix.net"
REMOTE_HOSTS = {
    "bequiet": RemoteHostEntry(
        docker_url="tcp://10.88.0.2:2375", reach_host="10.88.0.2"
    )
}


def _component(
    component_id: str = "hexarchy", host: str = "bequiet"
) -> ComponentConfig:
    return ComponentConfig(
        id=component_id,
        image="ghcr.io/org/app:main",
        container_name=component_id,
        host=host,
        ports=[PortMapping(host=8000, container=8000)],
        named_volumes=[f"{component_id}-data"],
        mounts=[VolumeMount(host=f"{component_id}-config", container="/config")],
    )


def _store(*configs: ComponentConfig) -> MagicMock:
    store = MagicMock()
    by_id = {c.id: c for c in configs}
    store.get.side_effect = by_id.get
    store.all.return_value = list(configs)
    return store


def _facade(
    tmp_path: Path | None = None,
    *configs: ComponentConfig,
) -> tuple[MultiHostBackend, MagicMock, MagicMock]:
    """A facade over AsyncMock backends; returns (facade, local, remote)."""
    local = AsyncMock(spec=ExecutionBackend)
    remote = AsyncMock(spec=ExecutionBackend)
    facade = MultiHostBackend(
        local,
        REMOTE_HOSTS,
        make_remote=MagicMock(return_value=remote),
        gateway_base_domain=BASE,
        traefik_dynamic_dir=str(tmp_path) if tmp_path else "",
    )
    if configs:
        facade.bind_store(_store(*configs))
    return facade, local, remote


class TestRouting:
    async def test_unbound_store_routes_local(self):
        facade, local, remote = _facade()
        record = ServiceRecord(name="hexarchy")
        await facade.restart(record)
        local.restart.assert_awaited_once_with(record)
        remote.restart.assert_not_awaited()

    async def test_remote_component_routes_to_remote_backend(self):
        facade, local, remote = _facade(None, _component())
        record = ServiceRecord(name="hexarchy")
        await facade.restart(record)
        remote.restart.assert_awaited_once_with(record)
        local.restart.assert_not_awaited()

    async def test_sibling_record_routes_via_component_id(self):
        facade, _local, remote = _facade(None, _component())
        record = ServiceRecord(name="hexarchy-db", component_id="hexarchy")
        await facade.status(record)
        remote.status.assert_awaited_once_with(record)

    async def test_unknown_remote_host_falls_back_to_local(self):
        facade, local, _remote = _facade(None, _component(host="nonexistent"))
        record = ServiceRecord(name="hexarchy")
        await facade.restart(record)
        local.restart.assert_awaited_once_with(record)

    async def test_local_component_routes_local(self):
        facade, local, _remote = _facade(None, _component("board", host=""))
        record = ServiceRecord(name="board")
        await facade.stop(record)
        local.stop.assert_awaited_once_with(record)

    async def test_remote_backend_built_once_per_host(self):
        local = AsyncMock(spec=ExecutionBackend)
        make_remote = MagicMock(return_value=AsyncMock(spec=ExecutionBackend))
        facade = MultiHostBackend(local, REMOTE_HOSTS, make_remote)
        facade.bind_store(_store(_component()))
        record = ServiceRecord(name="hexarchy")
        await facade.restart(record)
        await facade.start(record)
        make_remote.assert_called_once_with("tcp://10.88.0.2:2375", "10.88.0.2")

    async def test_self_and_disk_operations_stay_local(self):
        facade, local, remote = _facade(None, _component())
        await facade.disk_df()
        await facade.inspect_self()
        local.disk_df.assert_awaited_once()
        local.inspect_self.assert_awaited_once()
        remote.disk_df.assert_not_awaited()


class TestVolumeRouting:
    async def test_named_volume_of_remote_component_routes_remote(self):
        facade, _local, remote = _facade(None, _component())
        await facade.measure_volume_bytes("hexarchy-data")
        remote.measure_volume_bytes.assert_awaited_once_with("hexarchy-data")

    async def test_mount_volume_of_remote_component_routes_remote(self):
        facade, _local, remote = _facade(None, _component())
        await facade.read_volume_file("hexarchy-config", "config.json", 1024)
        remote.read_volume_file.assert_awaited_once()

    async def test_unowned_volume_routes_local(self):
        facade, local, _remote = _facade(None, _component())
        await facade.measure_volume_bytes("claude-auth")
        local.measure_volume_bytes.assert_awaited_once_with("claude-auth")


class TestFragmentLifecycle:
    async def test_remote_deploy_writes_fragment(self, tmp_path: Path):
        config = _component()
        facade, _local, remote = _facade(tmp_path, config)
        record = ServiceRecord(name="hexarchy")
        await facade.deploy(record, config, "ghcr.io/org/app:main")
        remote.deploy.assert_awaited_once()
        fragment = tmp_path / "remote-hexarchy.yml"
        assert fragment.exists()
        assert "10.88.0.2:8000" in fragment.read_text(encoding="utf-8")

    async def test_local_deploy_removes_stale_fragment(self, tmp_path: Path):
        # A component moved back from a remote host must lose its file route
        # (its labels take over) — otherwise the edge keeps a dead backend.
        stale = tmp_path / "remote-board.yml"
        stale.write_text("http: {}\n", encoding="utf-8")
        config = _component("board", host="")
        facade, local, _remote = _facade(tmp_path, config)
        await facade.deploy(ServiceRecord(name="board"), config, "img")
        local.deploy.assert_awaited_once()
        assert not stale.exists()

    async def test_remote_remove_container_removes_fragment(self, tmp_path: Path):
        config = _component()
        facade, _local, remote = _facade(tmp_path, config)
        record = ServiceRecord(name="hexarchy")
        await facade.deploy(record, config, "img")
        await facade.remove_container(record)
        remote.remove_container.assert_awaited_once_with(record)
        assert not (tmp_path / "remote-hexarchy.yml").exists()


class TestRemoteCreateContainer:
    """Remote-mode _create_container: published tunnel ports, no labels/network."""

    @pytest.fixture
    def remote_backend(self):
        dm = MagicMock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend(remote_bind_ip="10.88.0.2")
            yield b, client

    def test_remote_create_publishes_ports_on_bind_ip_only(self, remote_backend):
        b, client = remote_backend
        config = _component()
        b._create_container(config, "ghcr.io/org/app:main")
        kwargs = client.containers.create.call_args.kwargs
        assert kwargs["ports"] == {"8000/tcp": ("10.88.0.2", 8000)}
        assert kwargs["labels"] == {}
        assert kwargs["network"] is None

    def test_local_create_keeps_label_routing(self):
        dm = MagicMock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend(gateway_base_domain=BASE)
            b._create_container(_component(host=""), "ghcr.io/org/app:main")
        kwargs = client.containers.create.call_args.kwargs
        assert kwargs["ports"] == {}
        assert kwargs["labels"]["traefik.enable"] == "true"
        assert kwargs["network"] == "central-deploy-proxy"
