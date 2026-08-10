"""Chat agent deploy and update endpoints.

Extracted from chat_services.py — the deploy-family handlers
(chat_update_service, chat_deploy_service, chat_deploy) and
_resolve_deploy_contract.  The shared deploy staging sequence
(lock → backend.deploy → record update → sibling fan-out) is
factored into ``_execute_deploy_staging`` to eliminate drift
across the three handlers.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status

from robotsix_central_deploy.lifecycle._config_utils import (
    _merge_config,
)
from robotsix_central_deploy.lifecycle.deps.seed import (
    _build_component_config_from_spec,
    _namespace_spec_volumes,
    _validate_config_or_422,
)

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_store,
)
from ..models import DeployOutcome, ServiceRecord, ServiceState
from ..schemas import (
    ChatAgentDeployRequest,
    ChatAgentDeployResponse,
    ChatAgentServiceDeployResponse,
    ChatAgentUpdateResponse,
)
from ..store import ServiceStore
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ._sibling_utils import (
    _fanout_siblings_deploy_best_effort,
)

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Shared deploy staging helper
# ---------------------------------------------------------------------------


async def _execute_deploy_staging(
    *,
    name: str,
    record: ServiceRecord,
    config: ComponentConfig,
    image: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    audit_store: ChatAgentAuditStore,
    action: str,
    log_prefix: str,
    env_store: EnvStore | None = None,
) -> tuple[DeployOutcome, list[str]]:
    """Shared deploy staging: lock → backend.deploy → record update → sibling fan-out.

    Acquires the per-service deploy lock, calls ``backend.deploy``,
    updates *record* state/image/digests, persists via *store*, and fans
    out deploy to siblings (best-effort).  The caller owns pre-staging
    (config resolution, env merge, idempotency) and post-staging
    (audit append, response construction).
    """
    if not await try_acquire_deploy_lock(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{name}'.",
        )

    try:
        outcome = await backend.deploy(record, config, image)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s %s failed", log_prefix, _sanitize_log(name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=action,
                detail=f"{action.capitalize()} failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{action.capitalize()} failed: {exc}",
        )
    finally:
        release_deploy_lock(name)

    record.state = outcome.state
    record.image = image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    deployed_siblings = await _fanout_siblings_deploy_best_effort(
        name,
        config,
        store,
        backend,
        log_prefix,
        env_store=env_store,
    )

    return outcome, deployed_siblings


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/update
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/update",
    response_model=ChatAgentUpdateResponse,
    summary="Pull + recreate (deploy) an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_update_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentUpdateResponse:
    """Pull the latest image and recreate the container for an allowlisted service.

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one update per 300 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "update")

    # Guard: central-deploy cannot update itself through this path.
    # Self-updates must use the detached updater via
    # POST /chat/services/central-deploy/update.
    if name == "central-deploy":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    "Cannot update central-deploy through this endpoint. "
                    "The management plane cannot safely replace itself from inside. "
                    "Use POST /chat/services/central-deploy/update for self-updates."
                ),
            },
        )

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}'.",
        )

    # Merge env overrides from the env store (same as the main deploy endpoint).
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    # Inject the deploy API key when chat access is enabled.
    config = config.model_copy(update={"env": merged_env})

    outcome, updated_siblings = await _execute_deploy_staging(
        name=name,
        record=record,
        config=config,
        image=config.image,
        store=store,
        backend=backend,
        audit_store=audit_store,
        action="update",
        log_prefix="chat update",
        env_store=env_store,
    )

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="update",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
                + (
                    f"; siblings: {', '.join(updated_siblings)}"
                    if updated_siblings
                    else ""
                )
            ),
        )
    )

    return ChatAgentUpdateResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        detail="Update completed."
        + (f" Also updated: {', '.join(updated_siblings)}" if updated_siblings else ""),
        updated_siblings=updated_siblings,
    )


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/deploy
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/deploy",
    response_model=ChatAgentServiceDeployResponse,
    summary="First-boot deploy an already-registered component (idempotent)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_deploy_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentServiceDeployResponse:
    """Deploy an already-registered component for the first time.

    Brings a previously-registered (but not yet started) component up
    by pulling its image, creating its container, and starting it.

    Idempotent: if the component is already running the endpoint returns
    a clear success response without re-deploying.

    Access is gated by the same allowlist used by restart and update
    (``chat_agent_mutatable`` / ``allow_chat_access``).
    Rate-limited to one deploy per 300 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "deploy")

    # Guard: central-deploy cannot deploy itself through this path.
    # Self-updates must use the detached updater via
    # POST /chat/services/central-deploy/update.
    if name == "central-deploy":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    "Cannot deploy central-deploy through this endpoint. "
                    "The management plane cannot safely replace itself from inside. "
                    "Use POST /chat/services/central-deploy/update for self-updates."
                ),
            },
        )

    record = await _get_or_create_record(name, store)

    # Idempotency: if the component is already running, return success
    # without re-deploying (prevents duplicate containers).
    if record.state == ServiceState.RUNNING:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="deploy",
                detail="Deploy requested but component is already running (idempotent).",
            )
        )
        return ChatAgentServiceDeployResponse(
            name=name,
            deployed_digest=record.deployed_image_digest,
            previous_digest=record.previous_image_digest,
            current_state=record.state.value,
            health=record.health,
            detail="Component is already running.",
        )

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}'.",
        )

    # Merge env overrides from the env store.
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    # Inject the deploy API key when chat access is enabled.
    config = config.model_copy(update={"env": merged_env})

    outcome, deployed_siblings = await _execute_deploy_staging(
        name=name,
        record=record,
        config=config,
        image=config.image,
        store=store,
        backend=backend,
        audit_store=audit_store,
        action="deploy",
        log_prefix="chat deploy",
        env_store=env_store,
    )

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="deploy",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
                + (
                    f"; siblings: {', '.join(deployed_siblings)}"
                    if deployed_siblings
                    else ""
                )
            ),
        )
    )

    detail = "Deploy completed."
    if deployed_siblings:
        detail += f" Siblings deployed: {', '.join(deployed_siblings)}"

    return ChatAgentServiceDeployResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        health=record.health,
        detail=detail,
        deployed_siblings=deployed_siblings,
    )


# ---------------------------------------------------------------------------
# POST /chat/deploy
# ---------------------------------------------------------------------------


async def _resolve_deploy_contract(
    body: ChatAgentDeployRequest,
    request: Request,
    lifecycle_config: LifecycleConfig,
    component_config_store: ComponentConfigStore,
    registry: ComponentRegistry,
    backend: ExecutionBackend,
) -> ComponentConfig:
    """Fetch repo, parse compose + config schema, build & persist ComponentConfig.

    Returns the resolved ``ComponentConfig``.  Side-effects:
    persists to ``component_config_store``, registers in the in-memory
    ``registry``, writes merged config.json to the config volume, and
    seeds the ``EnvStore`` from the repo's env contract.
    """
    # --- Fetch repo, parse compose, validate config standard ---
    loop = asyncio.get_running_loop()
    from robotsix_central_deploy.lifecycle.deps._compose_resolver import (
        _resolve_compose_backbone,
    )

    _repo_files, derived_spec = await _resolve_compose_backbone(
        body.repo, body.name, lifecycle_config, loop
    )

    # --- Namespace volume names ---
    derived_spec = _namespace_spec_volumes(derived_spec, body.name)

    # --- Build ComponentConfig from DerivedSpec ---
    comp_cfg = _build_component_config_from_spec(derived_spec, git_url=body.repo)

    # Persist the config so future deploys (and sibling fan-out,
    # dashboard, etc.) can reference it.
    await component_config_store.put(comp_cfg)
    # Register in the in-memory loader so the gateway can route to it.
    registry.register(comp_cfg)

    # --- Write merged config.json to the config volume ---
    if derived_spec.config_schema is not None:
        config_yaml_store = await _get_config_yaml_store(request)
        await config_yaml_store.save_template(body.name, derived_spec.config_schema)
        # Use example values as base when present; otherwise empty.
        base_values: dict[str, object] = {}
        if derived_spec.config_example_values is not None:
            base_values = dict(derived_spec.config_example_values)
        try:
            merged_config = _merge_config(derived_spec.config_schema, base_values, {})
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": str(exc)},
            )
        _validate_config_or_422(derived_spec.config_schema, merged_config)
        if derived_spec.config_volume is not None:
            try:
                await backend.write_config_to_volume(
                    derived_spec.config_volume, merged_config
                )
            except Exception:
                # Roll back the schema we just stored — a component whose
                # config could not be written must not be left half-onboarded.
                await config_yaml_store.delete(body.name)
                raise

    # --- Seed EnvStore from the repo's env contract ---
    env_store = await _get_env_store(request)
    existing_env = await env_store.get(body.name)
    if not existing_env.env and not existing_env.secret_tokens:
        seeded_env = {k: v for k, v in derived_spec.env.items() if v}
        seeded_secrets = {k: "" for k, v in derived_spec.env.items() if not v}
        if seeded_env or seeded_secrets:
            await env_store.upsert(body.name, seeded_env, seeded_secrets)

    return comp_cfg


@router.post(
    "/chat/deploy",
    response_model=ChatAgentDeployResponse,
    summary="Contract-aware deploy: fetch docker-compose.yml and deploy an allowlisted component",
    responses={
        403: {"description": "Component not in the deploy allowlist"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_deploy(
    body: ChatAgentDeployRequest,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentDeployResponse:
    """Deploy an allowlisted component by fetching and parsing its deploy contract.

    Fetches the repo's ``deploy/docker-compose.yml``, resolves the
    image and full service topology (including siblings), and deploys
    every service — matching the dashboard onboarding flow.

    The component does NOT need a pre-existing ``ComponentConfig``;
    one is derived from the deploy contract on first deploy.
    Access is gated by the ``chat_agent_deployable_components`` server-
    level allowlist (``LifecycleConfig``).

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one deploy per 300 seconds per component.
    """
    lifecycle_config = await _get_config(request)
    if body.name not in lifecycle_config.chat_agent_deployable_components:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Component '{body.name}' is not in the deploy allowlist.",
        )

    _check_rate_limit(request.app.state, body.name, "deploy")

    # Resolve the deploy contract when no persisted config exists.
    comp_cfg = component_config_store.get(body.name)
    if comp_cfg is None:
        comp_cfg = await _resolve_deploy_contract(
            body,
            request,
            lifecycle_config,
            component_config_store,
            registry,
            backend,
        )

    # --- Merge env overrides and secrets ---
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(body.name, comp_cfg.env)
    # Inject the deploy API key when chat access is enabled.
    comp_cfg = comp_cfg.model_copy(update={"env": merged_env})

    # --- Get or create the service record ---
    record = await store.get(body.name)
    if record is None:
        record = ServiceRecord(name=body.name)
        await store.put(record)

    deploy_image = comp_cfg.image

    # --- Pre-seed sibling records so _get_sibling_pairs can find them ---
    if comp_cfg.siblings:
        for sib in comp_cfg.siblings:
            sib_name = f"{body.name}-{sib.service_key}"
            sib_record = ServiceRecord(
                name=sib_name,
                container_name=sib.container_name,
                image=sib.image,
                component_id=body.name,
            )
            await store.put(sib_record)

    outcome, deployed_siblings = await _execute_deploy_staging(
        name=body.name,
        record=record,
        config=comp_cfg,
        image=deploy_image,
        store=store,
        backend=backend,
        audit_store=audit_store,
        action="deploy",
        log_prefix="chat deploy",
        env_store=env_store,
    )

    # Update the persisted ComponentConfig.image so future dashboard-
    # initiated deploys use the correct image reference.
    if comp_cfg.image != deploy_image:
        comp_cfg.image = deploy_image
        await component_config_store.put(comp_cfg)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=body.name,
            action="deploy",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
            ),
        )
    )

    detail = "Deploy completed."
    if deployed_siblings:
        detail += f" Siblings deployed: {', '.join(deployed_siblings)}"

    return ChatAgentDeployResponse(
        name=body.name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        detail=detail,
        deployed_siblings=deployed_siblings,
    )
