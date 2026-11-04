"""Sibling fan-out helpers for the deploy and rollback routes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ...registry import ComponentConfig, ServiceConfig
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from .._config_utils import (
    _sanitize_log,
)
from ..deps import (
    _get_sibling_pairs,
)
from ..models import (
    ServiceRecord,
)
from ..store import ServiceStore

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fanout helpers
# ---------------------------------------------------------------------------


def _build_sibling_config(
    sib_config: ServiceConfig,
    sib_name: str,
    merged_env: dict[str, str],
) -> ComponentConfig:
    """Build a ComponentConfig for a sibling service from its ServiceConfig and merged env."""
    return ComponentConfig(
        id=sib_name,
        image=sib_config.image,
        container_name=sib_config.container_name,
        ports=sib_config.ports,
        mounts=sib_config.mounts,
        env=merged_env,
        health_check=sib_config.health_check,
        claude_mount=sib_config.claude_mount,
        claude_mount_path=sib_config.claude_mount_path,
        host_docker_sock=sib_config.host_docker_sock,
        named_volumes=[m.host for m in sib_config.mounts],
        command=sib_config.command,
        entrypoint=sib_config.entrypoint,
        tmpfs=sib_config.tmpfs,
        mem_limit=sib_config.mem_limit,
        memswap_limit=sib_config.memswap_limit,
        user=sib_config.user,
    )


async def _fanout_sibling_action(
    name: str,
    store: ServiceStore,
    registry: ComponentRegistry,
    env_store: EnvStore,
    *,
    action: Callable[
        [ServiceConfig, ServiceRecord, str, ComponentConfig], Awaitable[None]
    ],
    action_label: str,
    consumed_scopes: list[str] | None = None,
) -> None:
    """Fan out an action to all siblings of *name* (best-effort per sibling).

    Fetches the fresh config, iterates sibling pairs, resolves merged env,
    builds an effective ComponentConfig, and calls *action* for each sibling.
    Exceptions are caught and logged as warnings so one failing sibling does
    not block the others.
    """
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        # Siblings inherit the parent's consumed-scope resolution.
        if consumed_scopes:
            cred_env = await env_store.resolve_consumed_credentials(
                sib_name, consumed_scopes
            )
            if cred_env:
                merged_env = {**cred_env, **merged_env}
        effective_sib = _build_sibling_config(sib_config, sib_name, merged_env)
        try:
            await action(sib_config, sib_record, sib_name, effective_sib)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s sibling '%s' failed",
                action_label,
                _sanitize_log(sib_name),
            )
