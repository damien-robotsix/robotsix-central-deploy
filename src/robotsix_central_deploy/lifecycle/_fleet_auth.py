"""Fleet-auth hostname reconciliation.

When a component's chat access is enabled, its public UI hostname is
automatically added to the chat agent's ``fleet_auth.auth_hosts``
allowlist so the chat agent's ``http_probe`` / ``render_url`` tools can
reach the authenticated UI with server-side-injected credentials.
When access is disabled, the hostname is removed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

    from ..registry.config_store import ComponentConfigStore

logger = logging.getLogger(__name__)


async def _rebuild_fleet_auth_hosts(
    component_config_store: ComponentConfigStore,
    backend: Any,
    gateway_base_domain: str,
) -> None:
    """Core logic: rebuild ``fleet_auth.auth_hosts`` on chat-agent components.

    *backend* must have a ``write_config_to_volume`` method (the
    ``ExecutionBackend`` abstract interface).  *gateway_base_domain* is
    the base domain for constructing UI hostnames.

    Reads current config from each component's config volume (the source
    of truth) rather than from ``ConfigYamlStore``, which no longer
    stores config *values*.
    """
    if not gateway_base_domain:
        logger.debug("gateway_base_domain not set — skipping fleet_auth_hosts sync")
        return

    # Build the set of hostnames for all chat-accessible components.
    expected_hosts: set[str] = set()
    for cfg in component_config_store.all():
        if cfg.allow_chat_access or cfg.chat_agent_mutatable:
            host = f"{cfg.id}.{gateway_base_domain}"
            expected_hosts.add(host)

    domain_suffix = f".{gateway_base_domain}"

    # For each chat-agent component (chat_agent_mutatable=True), update
    # its fleet_auth.auth_hosts.
    for cfg in component_config_store.all():
        if not cfg.chat_agent_mutatable:
            continue

        # Read current config from the volume (source of truth).
        volume_config: dict[str, Any] = {}
        if cfg.config_volume:
            try:
                volume_config = await backend.read_config_from_volume(cfg.config_volume)
            except Exception:  # noqa: BLE001 — best-effort read; any failure should degrade gracefully
                volume_config = {}
        if not isinstance(volume_config, dict):
            volume_config = {}

        fleet_auth = volume_config.get("fleet_auth")
        if not isinstance(fleet_auth, dict):
            fleet_auth = {}
            volume_config["fleet_auth"] = fleet_auth

        existing_hosts: list[str] = fleet_auth.get("auth_hosts", [])
        if not isinstance(existing_hosts, list):
            existing_hosts = []

        # Preserve manual entries: anything that does NOT end with the
        # gateway domain suffix is an operator-managed host.
        manual_hosts = [
            h
            for h in existing_hosts
            if isinstance(h, str) and not h.endswith(domain_suffix)
        ]

        # Build the new list: manual entries first, then sorted auto-managed.
        new_hosts = manual_hosts + sorted(expected_hosts)

        if new_hosts == existing_hosts:
            continue  # No change needed.

        fleet_auth["auth_hosts"] = new_hosts
        volume_config["fleet_auth"] = fleet_auth

        # Write back to the component's config volume so it takes effect
        # immediately in the running component.
        if cfg.config_volume:
            try:
                await backend.write_config_to_volume(cfg.config_volume, volume_config)
            except Exception:
                logger.warning(
                    "Could not write fleet_auth to config volume for %s",
                    cfg.id,
                    exc_info=True,
                )

        logger.info(
            "Updated fleet_auth.auth_hosts for %s: %s",
            cfg.id,
            new_hosts,
        )


async def reconcile_fleet_auth_hosts(
    component_config_store: ComponentConfigStore,
    request: Request,
) -> None:
    """Route-handler wrapper: extract config from *request* and delegate.

    Called after every ``allow_chat_access`` or ``chat_agent_mutatable``
    toggle so the chat agent's fleet-auth allowlist stays in sync.
    Failures are logged but never raised — fleet-auth hosts degrade
    gracefully to the last-known-good set.
    """
    try:
        gateway_base_domain: str = getattr(
            request.app.state.config, "gateway_base_domain", ""
        )
        await _rebuild_fleet_auth_hosts(
            component_config_store,
            request.app.state.backend,
            gateway_base_domain,
        )
    except Exception:
        logger.warning(
            "Fleet-auth host reconciliation failed",
            exc_info=True,
        )
