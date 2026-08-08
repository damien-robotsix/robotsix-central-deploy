"""Service maintenance endpoints (refresh-contract, delete)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...onboard.port_utils import (
    collect_occupied_host_ports,
    preserve_host_port_assignments,
)
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
    _build_component_config_from_spec,
    _fetch_component_repo_files,
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_env_store,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
    _namespace_spec_volumes,
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
    fields (``repo_id``, ``caretaker_auto_update``, ``mem_limit``,
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
    from robotsix_central_deploy.onboard.parser import (
        ParseError,
        parse_compose,
    )

    comp_cfg, repo_files = await _fetch_component_repo_files(
        name, component_config_store
    )

    loop = asyncio.get_running_loop()

    if repo_files.compose_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Repo of '{name}' has no deploy/docker-compose.yml — "
                "the component must commit a deploy contract first"
            ),
        )

    try:
        spec = await loop.run_in_executor(
            None, parse_compose, repo_files.compose_bytes, name, comp_cfg.git_url
        )
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"deploy/docker-compose.yml parse failed: {'; '.join(exc.violations)}",
        ) from exc

    # parse_compose only reads the compose file; the config schema is a
    # separate repo file, so it has to be attached here. Without this the
    # save_template call below is unreachable and the stored template stays
    # pinned to whatever the component shipped at onboarding — the dashboard
    # editor and PUT /chat/config/{name} then silently drop every key the
    # component has added since (they walk the template's properties).
    if repo_files.config_schema_json is not None:
        try:
            spec.config_schema = json.loads(repo_files.config_schema_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"config/config.schema.json is not valid JSON: {exc}",
            ) from exc

    # Namespace volume names (same as onboard confirm)
    spec = _namespace_spec_volumes(spec, name)

    # Carry over host-port assignments before building the config. The manifest
    # states the port the repo author picked; the port this component actually
    # runs on was assigned at onboarding and may have been shifted to dodge a
    # collision. Re-reading the manifest previously reset it, which silently
    # pointed two components at the same host port.
    occupied = collect_occupied_host_ports(
        component_config_store, lifecycle_config.port, exclude_id=name
    )
    preserve_host_port_assignments(spec.ports, comp_cfg.ports, occupied)
    old_sibling_ports = {sib.service_key: sib.ports for sib in comp_cfg.siblings}
    for sib in spec.siblings:
        preserve_host_port_assignments(
            sib.ports, old_sibling_ports.get(sib.service_key, []), occupied
        )

    # Build the new ComponentConfig from the DerivedSpec (same logic as onboard confirm).
    # Preserve operator-set / system-set fields from the existing config.
    #
    # mem_limit, allow_chat_access and claude_mount are settable by the operator
    # through PUT /services/{name}/env, so the stored value outranks whatever the
    # manifest's labels imply — the manifest simply has no way to know an operator
    # turned chat access on. Omitting them here reset them to the label defaults:
    # claude_mount flipping back to false strips a component's claude-auth volume
    # on its next deploy, and allow_chat_access drops it from the chat roster.
    new_config = _build_component_config_from_spec(
        spec,
        git_url=comp_cfg.git_url,
        repo_id=comp_cfg.repo_id,
        caretaker_auto_update=comp_cfg.caretaker_auto_update,
        mem_limit=comp_cfg.mem_limit,
        allow_chat_access=comp_cfg.allow_chat_access,
        claude_mount=comp_cfg.claude_mount,
    )

    # Diff: collect which contract-derived fields changed.
    _CONTRACT_FIELDS = (
        "image",
        "container_name",
        "ports",
        "mounts",
        "env",
        "health_check",
        "command",
        "entrypoint",
        "tmpfs",
        "mem_limit",
        "claude_mount",
        "claude_mount_path",
        "host_docker_sock",
        "named_volumes",
        "siblings",
        "config_volume",
        "config_assist_command",
        "config_assist_seeds",
        "llmio_tier_level",
        "allow_chat_access",
        "user",
    )
    changed: list[str] = []
    previous: dict[str, Any] = {}
    current: dict[str, Any] = {}
    for field in _CONTRACT_FIELDS:
        old_val = getattr(comp_cfg, field)
        new_val = getattr(new_config, field)
        if old_val != new_val:
            changed.append(field)
            # Serialize model fields for the response
            if hasattr(old_val, "model_dump"):
                previous[field] = old_val.model_dump()
            elif (
                isinstance(old_val, list)
                and old_val
                and hasattr(old_val[0], "model_dump")
            ):
                previous[field] = [v.model_dump() for v in old_val]
            else:
                previous[field] = old_val
            if hasattr(new_val, "model_dump"):
                current[field] = new_val.model_dump()
            elif (
                isinstance(new_val, list)
                and new_val
                and hasattr(new_val[0], "model_dump")
            ):
                current[field] = [v.model_dump() for v in new_val]
            else:
                current[field] = new_val

    # Persist the updated config
    await component_config_store.put(new_config)
    registry.register(new_config)

    # If the config schema changed (new or removed), refresh the stored template.
    if spec.config_schema is not None:
        if spec.config_schema != await config_yaml_store.get_template(name):
            changed.append("config_schema")
        await config_yaml_store.save_template(name, spec.config_schema)
    # Note: we do NOT remove the template if the schema is now absent —
    # the operator may still want the old schema in the dashboard.

    logger.info(
        "Refreshed contract for %s from repo: %d field(s) changed (%s)",
        _sanitize_log(name),
        len(changed),
        ", ".join(changed) if changed else "none",
    )
    return ContractRefreshResponse(
        name=name,
        changed_fields=changed,
        previous=previous,
        current=current,
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
