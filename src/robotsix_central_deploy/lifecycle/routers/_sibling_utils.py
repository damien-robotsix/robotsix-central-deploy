"""Shared sibling fan-out helpers.

Extracted from services.py and chat.py to eliminate duplicated best-effort
fan-out boilerplate for start / stop / restart lifecycle actions.
"""

from __future__ import annotations

import logging
from typing import Literal

from ...registry.env_store import EnvStore
from ...registry.models import ComponentConfig
from .._config_utils import _sanitize_log, inject_deploy_api_key
from ..backends import ExecutionBackend
from ..deps import _get_sibling_pairs
from ..store import ServiceStore

logger = logging.getLogger(__name__)


async def _fanout_siblings_best_effort(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    action: Literal["start", "stop", "restart"],
) -> None:
    """Fan out *action* to every sibling of *name* (best-effort per sibling).

    Each sibling is handled independently — a failure in one does not
    prevent the others from being processed.  Missing sibling records
    are logged and skipped by ``_get_sibling_pairs``.
    """
    backend_method = getattr(backend, action)

    for sib, sib_record in await _get_sibling_pairs(name, config, store):
        try:
            final = await backend_method(sib_record)
            sib_record.state = final
            await store.put(sib_record)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s sibling '%s-%s' failed",
                action,
                _sanitize_log(name),
                _sanitize_log(sib.service_key),
            )


async def _fanout_siblings_deploy_best_effort(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    log_prefix: str,
    env_store: EnvStore | None = None,
) -> list[str]:
    """Fan out deploy to every sibling of *name* (best-effort per sibling).

    Each sibling is handled independently — a failure in one does not
    prevent the others from being deployed.  Missing sibling records
    are logged and skipped by ``_get_sibling_pairs``.

    When *env_store* is provided, per-sibling environment is merged
    via ``EnvStore.get_merged_env``; otherwise the sibling's own
    ``env`` dict is used directly.
    """
    deployed: list[str] = []
    for sib_cfg, sib_record in await _get_sibling_pairs(name, config, store):
        sib_name = f"{name}-{sib_cfg.service_key}"
        try:
            if env_store is not None:
                sib_env = await env_store.get_merged_env(sib_name, sib_cfg.env)
            else:
                sib_env = sib_cfg.env
            # Siblings inherit the parent's deploy API key when chat access is enabled.
            sib_effective = config.model_copy(
                update={
                    "id": sib_name,
                    "image": sib_cfg.image,
                    "container_name": sib_cfg.container_name,
                    "ports": sib_cfg.ports,
                    "mounts": sib_cfg.mounts,
                    "env": sib_env,
                    "health_check": sib_cfg.health_check,
                    "claude_mount": sib_cfg.claude_mount,
                    "claude_mount_path": sib_cfg.claude_mount_path,
                    "host_docker_sock": sib_cfg.host_docker_sock,
                    "named_volumes": [m.host for m in sib_cfg.mounts],
                    "command": sib_cfg.command,
                    "entrypoint": sib_cfg.entrypoint,
                    "tmpfs": sib_cfg.tmpfs,
                    "mem_limit": sib_cfg.mem_limit,
                    "user": sib_cfg.user,
                }
            )
            sib_outcome = await backend.deploy(sib_record, sib_effective, sib_cfg.image)
            sib_record.state = sib_outcome.state
            sib_record.image = sib_cfg.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
            deployed.append(sib_name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s: deploy sibling '%s' failed",
                log_prefix,
                _sanitize_log(sib_name),
            )
    return deployed
