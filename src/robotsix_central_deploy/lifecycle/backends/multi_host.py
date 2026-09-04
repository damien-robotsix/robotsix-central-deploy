"""Multi-host routing facade over per-host execution backends.

``MultiHostBackend`` implements the full :class:`ExecutionBackend`
interface and dispatches each operation to the backend of the host the
target component lives on:

- Service-keyed operations (start/stop/restart/status/logs/…) resolve the
  component's ``host`` field through a late-bound lookup into the
  component-config store.
- ``deploy``/``rollback`` route by the :class:`ComponentConfig` they are
  handed (authoritative), and additionally maintain the Traefik
  file-provider fragment that routes a remote component through the local
  edge.
- Volume-keyed operations resolve the owning component by scanning the
  store (named volumes, config volume, and mount names), falling back to
  the local host for unowned volumes (e.g. ``claude-auth``).
- Self-management, disk stats, and prunes always run locally: they act on
  the daemon central-deploy itself runs on.

Until the config store exists (early startup), everything routes to the
local backend — identical to single-host behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from ...registry.traefik_dynamic import remove_fragment, write_fragment
from ..models import (
    ComponentInspect,
    DeployOutcome,
    DockerDfStats,
    RollbackOutcome,
    SelfInspect,
    ServiceRecord,
    ServiceState,
)
from .base import ExecutionBackend

if TYPE_CHECKING:
    from ...registry.config_store import ComponentConfigStore
    from ...registry.models import ComponentConfig
    from ..config import RemoteHostEntry
    from ._util import PruneImagesResult

logger = logging.getLogger(__name__)


class MultiHostBackend(ExecutionBackend):
    """Route lifecycle operations to the right host's backend."""

    def __init__(
        self,
        local: ExecutionBackend,
        remote_hosts: dict[str, RemoteHostEntry],
        make_remote: Callable[[str, str], ExecutionBackend],
        gateway_base_domain: str = "",
        traefik_dynamic_dir: str = "",
    ) -> None:
        self._local = local
        self._remote_hosts = remote_hosts
        self._make_remote = make_remote
        self._gateway_base_domain = gateway_base_domain
        self._traefik_dynamic_dir = traefik_dynamic_dir
        self._remotes: dict[str, ExecutionBackend] = {}
        self._store: ComponentConfigStore | None = None

    # -- wiring --------------------------------------------------------------

    def bind_store(self, store: ComponentConfigStore) -> None:
        """Late-bind the component-config store used for host resolution.

        The store is created after the backend during startup; until it is
        bound, every operation routes to the local backend.
        """
        self._store = store

    # -- host resolution -----------------------------------------------------

    def _backend_for_host(self, host: str) -> ExecutionBackend:
        if not host:
            return self._local
        entry = self._remote_hosts.get(host)
        if entry is None or not entry.docker_url:
            logger.warning(
                "component targets unknown remote host %r — falling back to "
                "the local backend (configure it under remote_hosts)",
                host,
            )
            return self._local
        if host not in self._remotes:
            self._remotes[host] = self._make_remote(entry.docker_url, entry.reach_host)
        return self._remotes[host]

    def _host_of_component(self, component_id: str) -> str:
        if self._store is None or not component_id:
            return ""
        cfg = self._store.get(component_id)
        return cfg.host if cfg is not None else ""

    def _for_service(self, service: ServiceRecord) -> ExecutionBackend:
        return self._backend_for_host(
            self._host_of_component(service.component_id or service.name)
        )

    def _for_config(self, config: ComponentConfig) -> ExecutionBackend:
        return self._backend_for_host(config.host)

    def _for_volume(self, volume_name: str) -> ExecutionBackend:
        if self._store is None:
            return self._local
        for cfg in self._store.all():
            owned = set(cfg.named_volumes)
            if cfg.config_volume:
                owned.add(cfg.config_volume)
            owned.update(m.host for m in cfg.mounts)
            for sib in cfg.siblings:
                owned.update(m.host for m in sib.mounts)
            if volume_name in owned:
                return self._backend_for_host(cfg.host)
        return self._local

    # -- traefik fragment maintenance ---------------------------------------

    def _sync_fragment(self, config: ComponentConfig) -> None:
        """Write or remove the file-provider route for *config*.

        A local component must not carry a fragment (its labels route it),
        so moving a component back to the local host converges too.
        """
        if not self._traefik_dynamic_dir:
            return
        try:
            if config.host:
                entry = self._remote_hosts.get(config.host)
                reach = entry.reach_host if entry else ""
                write_fragment(
                    self._traefik_dynamic_dir,
                    config,
                    self._gateway_base_domain,
                    reach,
                )
            else:
                remove_fragment(self._traefik_dynamic_dir, config.id)
        except OSError:
            logger.exception(
                "failed to sync traefik fragment for %s — the component is "
                "deployed but may be unreachable through the edge",
                config.id,
            )

    # -- duck-typed passthroughs (lifespan/routers probe these with
    # getattr/hasattr and expect DockerSdkBackend's shape) --------------------

    @property
    def ghcr_credentials(self) -> Any:
        return self._local.ghcr_credentials  # type: ignore[attr-defined]

    @property
    def ghcr_pull_token(self) -> str:
        return getattr(self._local, "ghcr_pull_token", "")

    @ghcr_pull_token.setter
    def ghcr_pull_token(self, value: str) -> None:
        for b in (self._local, *self._remotes.values()):
            if hasattr(b, "ghcr_pull_token"):
                b.ghcr_pull_token = value

    @property
    def _client(self) -> Any:
        return self._local._client  # type: ignore[attr-defined]

    # -- service-keyed operations -------------------------------------------

    async def start(self, service: ServiceRecord) -> ServiceState:
        return await self._for_service(service).start(service)

    async def stop(self, service: ServiceRecord) -> ServiceState:
        return await self._for_service(service).stop(service)

    async def remove_container(self, service: ServiceRecord) -> None:
        backend = self._for_service(service)
        await backend.remove_container(service)
        if backend is not self._local and self._traefik_dynamic_dir:
            remove_fragment(
                self._traefik_dynamic_dir, service.component_id or service.name
            )

    async def restart(self, service: ServiceRecord) -> ServiceState:
        return await self._for_service(service).restart(service)

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        return await self._for_service(service).status(service)

    async def deploy(
        self,
        service: ServiceRecord,
        config: ComponentConfig,
        image_ref: str,
    ) -> DeployOutcome:
        outcome = await self._for_config(config).deploy(service, config, image_ref)
        self._sync_fragment(config)
        return outcome

    async def rollback(
        self,
        service: ServiceRecord,
        config: ComponentConfig,
    ) -> RollbackOutcome:
        return await self._for_config(config).rollback(service, config)

    def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        return self._for_service(service).stream_logs(
            service, tail=tail, since=since, follow=follow
        )

    async def get_container_logs(
        self,
        service: ServiceRecord,
        tail: int = 200,
    ) -> str:
        return await self._for_service(service).get_container_logs(service, tail=tail)

    async def get_container_diagnostics(self, service: ServiceRecord) -> dict[str, Any]:
        return await self._for_service(service).get_container_diagnostics(service)

    # -- local-only operations ----------------------------------------------

    async def disk_df(self) -> DockerDfStats:
        return await self._local.disk_df()

    async def prune_builds(self) -> int:
        return await self._local.prune_builds()

    async def prune_images(
        self, protected_refs: set[str], *, force: bool = False
    ) -> PruneImagesResult:
        return await self._local.prune_images(protected_refs, force=force)

    async def remove_stale_helpers(self) -> int:
        return await self._local.remove_stale_helpers()

    async def inspect_self(self) -> SelfInspect | None:
        return await self._local.inspect_self()

    async def heal_self_network_alias(self, network_name: str) -> str | None:
        return await self._local.heal_self_network_alias(network_name)

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        return await self._local.trigger_self_update(
            target, watchtower_image, docker_host_url, docker_api_version
        )

    async def trigger_self_restart(self, target: SelfInspect) -> str:
        return await self._local.trigger_self_restart(target)

    # -- claude-auth (a local volume by definition) ---------------------------

    async def check_claude_auth(self, volume_name: str) -> dict[str, Any]:
        return await self._local.check_claude_auth(volume_name)

    async def write_claude_credentials(
        self, volume_name: str, credentials_json: str
    ) -> dict[str, Any]:
        return await self._local.write_claude_credentials(volume_name, credentials_json)

    async def read_claude_credentials(self, volume_name: str) -> dict[str, Any]:
        return await self._local.read_claude_credentials(volume_name)

    # -- volume-keyed operations ----------------------------------------------

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        await self._for_volume(volume_name).write_config_to_volume(
            volume_name, config_dict
        )

    async def write_llmio_tier_config_to_volume(
        self, volume_name: str, tier_config: dict[str, Any]
    ) -> None:
        await self._for_volume(volume_name).write_llmio_tier_config_to_volume(
            volume_name, tier_config
        )

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        return await self._for_volume(volume_name).read_config_from_volume(volume_name)

    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        return await self._for_volume(volume_name).run_config_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=timeout_seconds,
        )

    async def prune_volume_files(
        self,
        volume_name: str,
        rel_path: str,
        glob: str,
        max_age_days: int,
    ) -> dict[str, int]:
        return await self._for_volume(volume_name).prune_volume_files(
            volume_name, rel_path, glob, max_age_days
        )

    async def measure_volume_bytes(self, volume_name: str) -> int | None:
        return await self._for_volume(volume_name).measure_volume_bytes(volume_name)

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        return await self._for_volume(volume_name).list_volume_dir(
            volume_name, rel_path
        )

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        return await self._for_volume(volume_name).read_volume_file(
            volume_name, rel_path, max_bytes
        )

    async def write_volume_file(
        self,
        volume_name: str,
        rel_path: str,
        content: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        return await self._for_volume(volume_name).write_volume_file(
            volume_name, rel_path, content, overwrite
        )

    async def remove_volume(self, volume_name: str) -> None:
        await self._for_volume(volume_name).remove_volume(volume_name)

    async def relocate_volume(
        self,
        volume_name: str,
        target_disk_path: str,
        container_user: str | None = None,
    ) -> dict[str, Any]:
        return await self._for_volume(volume_name).relocate_volume(
            volume_name, target_disk_path, container_user=container_user
        )
