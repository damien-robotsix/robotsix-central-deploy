"""Deploy-plane credential provisioning for chat-agent components.

A chat-agent component calls the deploy API with the plane's own API key.
That key is a **credential of the component's application**, so the config
standard puts it in the component's config file, not in ``environment:``
(config-standard §5 — first-party secrets never travel as env vars).

This writes ``central_deploy.api_token`` into the config volume of every
chat-agent component, the same way ``_fleet_auth`` maintains
``fleet_auth.auth_hosts``: read the volume, set the key, write it back
through the schema guard.  A component whose schema does not declare the
key is skipped, so the engine stays generic — it fills the block for
components that ask for it and leaves everyone else alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ._config_utils import ConfigWriteRejected, write_config_to_volume_checked

if TYPE_CHECKING:
    from fastapi import Request

    from ..registry.config_store import ComponentConfigStore

logger = logging.getLogger(__name__)

#: Canonical config location of the deploy-plane API token.
_BLOCK = "central_deploy"
_KEY = "api_token"


async def _rebuild_deploy_credential(
    component_config_store: "ComponentConfigStore",
    backend: Any,
    api_key: str,
    config_yaml_store: Any = None,
) -> None:
    """Write the deploy API key into each chat-agent component's config.

    *backend* must provide ``read_config_from_volume`` /
    ``write_config_to_volume`` (the ``ExecutionBackend`` interface).
    No-op when *api_key* is empty.
    """
    if not api_key:
        logger.debug("Deploy API key not configured — skipping credential sync")
        return

    for cfg in component_config_store.all():
        if not cfg.chat_agent_mutatable or not cfg.config_volume:
            continue

        volume_config: dict[str, Any] = {}
        try:
            volume_config = await backend.read_config_from_volume(cfg.config_volume)
        except Exception:
            volume_config = {}
        if not isinstance(volume_config, dict):
            volume_config = {}

        block = volume_config.get(_BLOCK)
        if not isinstance(block, dict):
            block = {}

        if block.get(_KEY) == api_key:
            continue  # Already provisioned.

        block[_KEY] = api_key
        volume_config[_BLOCK] = block

        try:
            if config_yaml_store is not None:
                await write_config_to_volume_checked(
                    backend,
                    config_yaml_store,
                    cfg.id,
                    cfg.config_volume,
                    volume_config,
                )
            else:
                await backend.write_config_to_volume(cfg.config_volume, volume_config)
        except ConfigWriteRejected:
            # The component does not declare the block — it is not a deploy
            # API consumer, or it names the credential somewhere else.
            logger.info(
                "Skipped deploy-credential write for %s — its schema does not "
                "declare %s.%s",
                cfg.id,
                _BLOCK,
                _KEY,
            )
            continue
        except Exception:
            logger.warning(
                "Could not write the deploy credential to the config volume for %s",
                cfg.id,
                exc_info=True,
            )
            continue

        logger.info("Provisioned %s.%s for %s", _BLOCK, _KEY, cfg.id)


async def reconcile_deploy_credential(
    component_config_store: "ComponentConfigStore",
    request: "Request",
) -> None:
    """Route-handler wrapper: extract state from *request* and delegate.

    Failures are logged, never raised — a toggle route must not fail
    because a component's config volume was unreachable.
    """
    try:
        api_key: str = request.app.state.config.api_key.get_secret_value()
        await _rebuild_deploy_credential(
            component_config_store,
            request.app.state.backend,
            api_key,
            request.app.state.config_yaml_store,
        )
    except Exception:
        logger.warning("Deploy-credential reconciliation failed", exc_info=True)
