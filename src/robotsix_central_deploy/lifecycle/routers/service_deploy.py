"""Service deploy and rollback endpoints for the lifecycle server."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.params import Body

from ..auth import verify_auth
from ..backend import ExecutionBackend, collect_protected_image_refs
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_deploy_history_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
)
from ..models import (
    DeployHistoryEntry,
    DeployHistoryResponse,
    DeployRequest,
    DeployResponse,
    ErrorDetail,
    RollbackRequest,
    RollbackResponse,
    ServiceState,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)


def _sanitize(value: str) -> str:
    """Replace newlines to prevent log-injection (CWE-117)."""
    return value.replace("\n", "\\n").replace("\r", "\\r")


router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fanout_deploy_siblings(
    name: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
    deploy_history_store: DeployHistoryStore,
) -> None:
    """Deploy all siblings of *name* (best-effort per sibling)."""
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        effective_sib = ComponentConfig(
            id=sib_name,
            image=sib_config.image,
            container_name=sib_config.container_name,
            ports=sib_config.ports,
            mounts=sib_config.mounts,
            env=merged_env,
            health_check=sib_config.health_check,
            claude_mount=sib_config.claude_mount,
            host_docker_sock=sib_config.host_docker_sock,
            named_volumes=[m.host for m in sib_config.mounts],
            command=sib_config.command,
            entrypoint=sib_config.entrypoint,
        )
        try:
            sib_outcome = await backend.deploy(
                sib_record, effective_sib, sib_config.image
            )
            sib_record.state = sib_outcome.state
            sib_record.image = sib_config.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
            # Record sibling history (best-effort)
            try:
                await deploy_history_store.append(
                    sib_name,
                    DeployHistoryEntry(
                        digest=sib_outcome.deployed_digest,
                        image_ref=sib_config.image,
                        timestamp=time.time(),
                        source="manual",
                        previous_digest=sib_outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "deploy sibling '%s': failed to record history",
                    _sanitize(sib_name),
                    exc_info=True,
                )
        except Exception:
            logger.warning("deploy sibling '%s' failed", _sanitize(sib_name))


async def _fanout_rollback_siblings(
    name: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
) -> None:
    """Roll back all siblings of *name* (best-effort per sibling)."""
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        if not sib_record.previous_image_digest:
            logger.warning(
                "rollback sibling '%s-%s': no prior digest — skipping",
                _sanitize(name),
                _sanitize(sib_config.service_key),
            )
            continue
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        effective_sib = ComponentConfig(
            id=sib_name,
            image=sib_config.image,
            container_name=sib_config.container_name,
            ports=sib_config.ports,
            mounts=sib_config.mounts,
            env=merged_env,
            health_check=sib_config.health_check,
            claude_mount=sib_config.claude_mount,
            host_docker_sock=sib_config.host_docker_sock,
            named_volumes=[m.host for m in sib_config.mounts],
            command=sib_config.command,
            entrypoint=sib_config.entrypoint,
        )
        try:
            sib_outcome = await backend.rollback(sib_record, effective_sib)
            old_dep_sib = sib_record.deployed_image_digest
            old_prev_sib = sib_record.previous_image_digest
            sib_record.state = sib_outcome.state
            sib_record.deployed_image_digest = old_prev_sib
            sib_record.previous_image_digest = old_dep_sib
            sib_record.image_revision = old_prev_sib
            await store.put(sib_record)
        except Exception:
            logger.warning(
                "rollback sibling '%s-%s' failed",
                _sanitize(name),
                _sanitize(sib_config.service_key),
            )


# ---------------------------------------------------------------------------
# POST /services/{name}/deploy
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/deploy",
    response_model=DeployResponse,
    summary="Deploy a new image version for a service",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service or component config not found",
        },
        503: {"model": ErrorDetail, "description": "Registry not loaded"},
    },
)
async def deploy_service(
    name: str,
    request: Request,
    body: DeployRequest | None = Body(default=None),  # type: ignore[assignment]
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> DeployResponse:
    """Pull and deploy a new image version for a service.

    Optionally accepts a specific image reference; defaults to the component's
    configured image. Writes merged config.yaml to the config volume before
    starting when a config schema is present. Raises 404 if the service or
    component config is not found, 503 if the registry checker is not loaded,
    and 500 on backend failure. Sibling services are deployed on a best-effort
    basis. Persists the new and previous image digests to the store.
    """
    if body is None:
        body = DeployRequest()

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot deploy",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    image_ref = body.image or config.image

    # Write merged config.yaml into the config volume before starting the container.
    if config.has_config_yaml and config.config_volume:
        merged_cfg = await config_yaml_store.get_current(
            name
        ) or await config_yaml_store.get_template(name)
        if merged_cfg:
            try:
                await backend.write_config_to_volume(config.config_volume, merged_cfg)
            except Exception as exc:
                logger.warning(
                    "deploy %s: could not write config.yaml to volume %s: %s",
                    _sanitize(name),
                    config.config_volume,
                    exc,
                )
                # non-fatal: container may still start if config was written earlier

    try:
        outcome = await backend.deploy(record, config, image_ref)
    except Exception as exc:
        logger.exception("deploy %s failed", _sanitize(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deploy failed: {exc}",
        )

    record.state = outcome.state
    record.image = image_ref
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    record.last_error = ""
    await store.put(record)

    # Record deploy history (best-effort — never fail a successful deploy)
    try:
        await deploy_history_store.append(
            name,
            DeployHistoryEntry(
                digest=outcome.deployed_digest,
                image_ref=image_ref,
                timestamp=time.time(),
                source="manual",
                previous_digest=outcome.previous_digest,
            ),
        )
    except Exception:
        logger.warning(
            "deploy %s: failed to record history entry", _sanitize(name), exc_info=True
        )

    # Deploy siblings
    await _fanout_deploy_siblings(
        name, store, backend, registry, env_store, deploy_history_store
    )

    # Auto-prune dangling images left behind by the update (opt-in setting);
    # rollback targets recorded in the store are protected. Entirely
    # best-effort: a settings-store or prune failure must never fail the
    # deploy that already succeeded.
    try:
        settings_store = getattr(request.app.state, "settings_store", None)
        settings = await settings_store.get() if settings_store is not None else None
        if settings is not None and settings.image_auto_prune:
            protected = await collect_protected_image_refs(store)
            reclaimed = await backend.prune_images(protected)
            if reclaimed:
                logger.info(
                    "deploy %s: image auto-prune reclaimed %d bytes",
                    _sanitize(name),
                    reclaimed,
                )
    except Exception:
        logger.warning(
            "deploy %s: image auto-prune failed", _sanitize(name), exc_info=True
        )

    return DeployResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/rollback
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/rollback",
    response_model=RollbackResponse,
    summary="Rollback a service to a prior image digest",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "No prior digest recorded"},
        503: {"model": ErrorDetail},
    },
)
async def rollback_service(
    name: str,
    request: Request,
    body: RollbackRequest | None = Body(default=None),  # type: ignore[assignment]
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> RollbackResponse:
    """Roll back a service to a previously recorded image digest.

    When *digest* is absent/None: swaps the deployed and previous digests
    (the original one-step rollback). When *digest* is present: validates it
    appears in the component's deploy history, deploys that digest via the
    backend, and records the rollback.

    Raises 404 if the service or component config is not found, 409 if no
    prior digest or the target digest is not in history, and 500 on backend
    failure. Sibling services are rolled back on a best-effort basis
    (one-step behaviour only).
    """
    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot rollback",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    if body is None:
        body = RollbackRequest()

    # -- Target-digest rollback (multi-entry history) -----------------------
    if body.digest is not None:
        target_digest = body.digest
        history = await deploy_history_store.list(name)
        recorded_digests = {e.digest for e in history}
        if target_digest not in recorded_digests:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="digest not in deploy history",
            )

        # Derive a digest-pinned repo ref from config.image.
        #   [registry[:port]/]name[:tag|@digest]
        # Strip @digest, then split on the last '/' so ports are never
        # mistaken for tags, then strip the :tag from the name portion.
        bare = config.image.split("@", 1)[0]
        if "/" in bare:
            prefix, name = bare.rsplit("/", 1)
            name = name.rsplit(":", 1)[0] if ":" in name else name
            repo = f"{prefix}/{name}"
        else:
            repo = bare.rsplit(":", 1)[0] if ":" in bare else bare
        image_ref = f"{repo}@{target_digest}"

        try:
            deploy_outcome = await backend.deploy(record, config, image_ref)
        except Exception as exc:
            logger.exception("rollback %s failed", _sanitize(name))
            record.state = ServiceState.FAILED
            record.last_error = str(exc)
            await store.put(record)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Rollback failed: {exc}",
            )

        record.state = deploy_outcome.state
        record.deployed_image_digest = deploy_outcome.deployed_digest
        record.previous_image_digest = deploy_outcome.previous_digest
        record.image_revision = deploy_outcome.deployed_digest
        record.last_error = ""
        await store.put(record)

        try:
            await deploy_history_store.append(
                name,
                DeployHistoryEntry(
                    digest=deploy_outcome.deployed_digest,
                    image_ref=image_ref,
                    timestamp=time.time(),
                    source="rollback",
                    previous_digest=deploy_outcome.previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "rollback %s: failed to record history entry",
                _sanitize(name),
                exc_info=True,
            )

        # Fan out siblings (one-step; current behaviour)
        await _fanout_rollback_siblings(name, store, backend, registry, env_store)

        return RollbackResponse(
            name=name,
            rolled_back_to_digest=deploy_outcome.deployed_digest,
            current_state=record.state,
        )

    # -- Original one-step rollback -----------------------------------------
    if not record.previous_image_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No prior image digest recorded for '{name}' — run a deploy first",
        )

    # Snapshot current digests before mutating
    old_deployed = record.deployed_image_digest
    old_previous = record.previous_image_digest

    try:
        outcome = await backend.rollback(record, config)
    except Exception as exc:
        logger.exception("rollback %s failed", _sanitize(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback failed: {exc}",
        )

    # Swap digests: rolled-back-to becomes deployed; what-we-had becomes previous
    record.state = outcome.state
    record.deployed_image_digest = old_previous
    record.previous_image_digest = old_deployed
    record.image_revision = old_previous
    record.last_error = ""
    await store.put(record)

    # Rollback siblings using each sibling's previous_image_digest
    await _fanout_rollback_siblings(name, store, backend, registry, env_store)

    return RollbackResponse(
        name=name,
        rolled_back_to_digest=old_previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# GET /services/{name}/history
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/history",
    response_model=DeployHistoryResponse,
    summary="Get deploy history for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_history(
    name: str,
    store: ServiceStore = Depends(_get_store),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> DeployHistoryResponse:
    """Return the deploy history for a service, most-recent-first.

    Returns 200 with an empty ``entries`` list when no history is recorded.
    Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    entries = await deploy_history_store.list(name)
    return DeployHistoryResponse(name=name, entries=entries)
