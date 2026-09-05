"""Service maintenance endpoints (refresh-contract, delete)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_env_store,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
    refresh_component_contract,
)
from ..models import ErrorDetail
from ..schemas import ContractRefreshResponse
from ..store import ServiceStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _delete_component_volumes(
    name: str,
    config: ComponentConfig,
    pairs: list[tuple[Any, Any]],
    backend: ExecutionBackend,
) -> None:
    """Best-effort removal of volumes for *name* and its siblings."""
    volumes: list[str] = list(config.named_volumes)
    for sib_cfg, _sib_record in pairs:
        volumes.extend(m.host for m in sib_cfg.mounts)
    seen: set[str] = set()
    for vol in volumes:
        if vol in seen:
            continue
        seen.add(vol)
        logger.info(
            "delete %s: removing volume %s (remove_volumes=true)",
            _sanitize_log(name),
            _sanitize_log(vol),
        )
        try:
            await backend.remove_volume(vol)
        except Exception:
            logger.warning(
                "remove_volume failed for %s during delete of %s",
                _sanitize_log(vol),
                _sanitize_log(name),
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# POST /services/{name}/refresh-contract
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/refresh-contract",
    response_model=ContractRefreshResponse,
    summary="Refetch deploy/docker-compose.yml from the repo and update stored contract",
    responses={
        400: {
            "model": ErrorDetail,
            "description": "Component has no git_url",
        },
        404: {
            "model": ErrorDetail,
            "description": "Component not found or repo has no deploy/docker-compose.yml",
        },
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch failed or compose parse failed",
        },
    },
)
async def refresh_contract(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    lifecycle_config: LifecycleConfig = Depends(_get_config),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ContractRefreshResponse:
    """Re-parse the component's deploy/docker-compose.yml and update stored settings.

    Contract-derived fields (image, mounts, command, entrypoint, health check,
    siblings, labels, etc.) are refreshed from the repo HEAD.  Operator-set
    fields (``repo_id``, ``auto_update_enabled``, ``mem_limit``,
    ``allow_chat_access``, ``claude_mount``) and environment overrides in the
    EnvStore are left untouched, as are existing host-port assignments —
    the manifest's host ports are only honoured for container ports this
    component did not already expose.  The endpoint returns which fields
    changed so the operator can decide whether a redeploy is needed.

    The stored config *schema* is refreshed from the repo's
    ``config/config.schema.json`` too, and reported as a ``config_schema``
    entry in ``changed_fields`` (the schemas themselves are too large to
    include in the ``previous``/``current`` snapshots).
    """
    result = await refresh_component_contract(
        name,
        component_config_store,
        config_yaml_store,
        registry,
        lifecycle_config,
    )
    return ContractRefreshResponse(
        name=name,
        changed_fields=result.changed_fields,
        previous=result.previous,
        current=result.current,
    )


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


@router.delete(
    "/services/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an onboarded component and optionally its container",
)
async def delete_service(
    name: str,
    stop_container: bool = Query(
        default=True,
        description="Stop and remove the managed container (true) or leave it running (false)",
    ),
    remove_volumes: bool = Query(
        default=False,
        description="Also delete the component's data volumes (IRREVERSIBLE — destroys stored data)",
    ),
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    env_store: EnvStore = Depends(_get_env_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> None:
    """Remove an onboarded component and optionally its container and volumes.

    Deletes the service record, env/secrets, config.json, and component config.
    Optionally stops and removes the Docker container (``stop_container``) and
    deletes data volumes (``remove_volumes``, irreversible).  Idempotent —
    succeeds even when some persisted state is already absent (e.g. the
    component config was cleared).  Raises 404 only when *neither* a service
    record nor a component config exists for *name*.
    """
    # 1. Look up primary record and config independently (either may be absent)
    record = await store.get(name)
    config = config_store.get(name)

    # 2. If neither exists, there is nothing to tear down
    if record is None and config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # 3. Resolve sibling pairs (requires config; fall back to prefix scan)
    if config is not None:
        pairs = await _get_sibling_pairs(name, config, store)

    # 4. Best-effort container stop/remove (only when config is present)
    if stop_container and config is not None:
        if record is not None:
            try:
                await backend.stop(record)
            except Exception:
                logger.warning(
                    "stop failed for %s during delete",
                    _sanitize_log(name),
                    exc_info=True,
                )
            try:
                await backend.remove_container(record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete",
                    _sanitize_log(name),
                    exc_info=True,
                )
        for _sib_cfg, sib_record in pairs:
            try:
                await backend.stop(sib_record)
            except Exception:
                logger.warning(
                    "stop failed for %s during delete",
                    _sanitize_log(sib_record.name),
                    exc_info=True,
                )
            try:
                await backend.remove_container(sib_record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete",
                    _sanitize_log(sib_record.name),
                    exc_info=True,
                )

    # 4b. Best-effort volume removal (opt-in; IRREVERSIBLE; requires config).
    if remove_volumes and config is not None:
        await _delete_component_volumes(name, config, pairs, backend)

    # 5. Delete sibling records and env
    if config is not None:
        for sib_cfg, sib_record in pairs:
            await store.delete(sib_record.name)
            await env_store.delete(f"{name}-{sib_cfg.service_key}")
    else:
        # Discover siblings by prefix scan on the service store when
        # the component config is absent (e.g. already cleared).
        all_records = await store.list_all()
        for r in all_records:
            if r.name.startswith(f"{name}-"):
                await store.delete(r.name)
                await env_store.delete(r.name)

    # 6. Delete primary record
    if record is not None:
        await store.delete(name)

    # 7. Delete primary env/secrets
    await env_store.delete(name)

    # 8. Delete primary config.json
    await config_yaml_store.delete(name)

    # 9. Delete from config store (no-op if absent)
    await config_store.delete(name)

    # 10. Remove from in-memory registry (no-op if absent)
    registry.unregister(name)
