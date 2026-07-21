"""Chat agent service restart/update/deploy endpoints.

Extracted from chat.py — these are the scoped write endpoints that
allow the chat agent to restart, update (pull + recreate), or
generically deploy an allowlisted service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
)
from .._config_utils import _sanitize_log
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ._sibling_utils import _fanout_siblings_best_effort
from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..models import ActionType, ServiceRecord, ServiceState, can_transition
from ..schemas import (
    ChatAgentDeployRequest,
    ChatAgentDeployResponse,
    ChatAgentRestartResponse,
    ChatAgentUpdateResponse,
)
from ..store import ServiceStore
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/restart",
    response_model=ChatAgentRestartResponse,
    summary="Restart an allowlisted service (idempotent)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Invalid state transition"},
        429: {"description": "Rate limited"},
    },
)
async def chat_restart_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentRestartResponse:
    """Restart an allowlisted service. Idempotent.

    Raises 403 if the service is not in the chat-agent allowlist.
    Rate-limited to one restart per 60 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "restart")

    record = await _get_or_create_record(name, store)
    previous = record.state

    if record.state == ServiceState.RESTARTING:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail="Restart already in progress.",
            )
        )
        return ChatAgentRestartResponse(
            name=name,
            previous_state=previous.value,
            current_state=ServiceState.RESTARTING.value,
            detail="Restart already in progress",
        )

    if not can_transition(record.state, ServiceState.RESTARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot restart from state '{record.state.value}'",
        )

    record.state = ServiceState.RESTARTING
    await store.put(record)

    try:
        final_state = await backend.restart(record)
    except Exception as exc:
        logger.exception("chat restart %s failed", _sanitize_log(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail=f"Restart failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
    await store.put(record)

    # Restart siblings (best-effort).
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "restart")

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action=ActionType.RESTART,
            detail=f"Restarted: {previous.value} → {final_state.value}",
        )
    )

    return ChatAgentRestartResponse(
        name=name,
        previous_state=previous.value,
        current_state=record.state.value,
    )


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
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentUpdateResponse:
    """Pull the latest image and recreate the container for an allowlisted service.

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one update per 300 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "update")

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
    config = config.model_copy(update={"env": merged_env})

    # Serialise concurrent deploys.
    if not await try_acquire_deploy_lock(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{name}'.",
        )

    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("chat update %s failed", _sanitize_log(name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="update",
                detail=f"Update failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update failed: {exc}",
        )
    finally:
        release_deploy_lock(name)

    record.state = outcome.state
    record.image = config.image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Deploy siblings (best-effort) so the whole component group converges.
    updated_siblings: list[str] = []
    if config.siblings:
        for sib_cfg, sib_record in await _get_sibling_pairs(name, config, store):
            sib_name = f"{name}-{sib_cfg.service_key}"
            try:
                sib_merged_env = await env_store.get_merged_env(sib_name, sib_cfg.env)
                sib_effective = config.model_copy(
                    update={
                        "id": sib_name,
                        "image": sib_cfg.image,
                        "container_name": sib_cfg.container_name,
                        "ports": sib_cfg.ports,
                        "mounts": sib_cfg.mounts,
                        "env": sib_merged_env,
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
                sib_outcome = await backend.deploy(
                    sib_record, sib_effective, sib_cfg.image
                )
                sib_record.state = sib_outcome.state
                sib_record.image = sib_cfg.image
                sib_record.deployed_image_digest = sib_outcome.deployed_digest
                sib_record.previous_image_digest = sib_outcome.previous_digest
                await store.put(sib_record)
                updated_siblings.append(sib_name)
            except Exception:
                logger.warning(
                    "chat update: deploy sibling '%s' failed",
                    _sanitize_log(sib_name),
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
# POST /chat/deploy
# ---------------------------------------------------------------------------


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
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
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
    import asyncio
    import json

    from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_repo_files
    from robotsix_central_deploy.onboard.parser import ParseError, parse_compose
    from robotsix_central_deploy.lifecycle.deps.seed import (
        _namespace_spec_volumes,
        _build_component_config_from_spec,
        _validate_config_or_422,
    )
    from robotsix_central_deploy.lifecycle._config_utils import (
        _canonical_hash,
        _merge_config,
    )

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
        # --- Fetch repo files (clone is blocking → run in executor) ---
        loop = asyncio.get_running_loop()

        github_token: str | None = None
        try:
            from .onboard import (
                _parse_github_owner_repo as _parse_gh,
            )

            parsed = _parse_gh(body.repo)
        except ImportError:
            parsed = None

        if (
            parsed is not None
            and lifecycle_config.github_app_id
            and lifecycle_config.github_app_private_key
        ):
            owner, repo_name = parsed
            try:
                from robotsix_central_deploy.lifecycle.github_app import (
                    get_installation_token_sync,
                )

                github_token = await loop.run_in_executor(
                    None,
                    get_installation_token_sync,
                    lifecycle_config.github_app_id,
                    lifecycle_config.github_app_private_key,
                    owner,
                    repo_name,
                )
            except Exception:
                safe_owner = owner.replace("\n", "_").replace("\r", "_")
                safe_repo = repo_name.replace("\n", "_").replace("\r", "_")
                logger.warning(
                    "Cannot get GitHub App installation token for %s/%s; "
                    "cloning unauthenticated (public repos only)",
                    safe_owner,
                    safe_repo,
                )

        try:
            repo_files = await loop.run_in_executor(
                None, fetch_repo_files, body.repo, 30, github_token
            )
        except FetchError as e:
            raise HTTPException(status_code=422, detail={"error": str(e)})

        # --- Parse deploy/docker-compose.yml ---
        try:
            derived_spec = parse_compose(repo_files.compose_bytes, body.name, body.repo)
        except ParseError as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "compose validation failed",
                    "violations": e.violations,
                },
            )

        # --- Parse config/config.schema.json if present ---
        if repo_files.config_schema_json is not None:
            try:
                derived_spec.config_schema = json.loads(repo_files.config_schema_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": (
                            f"config/config.schema.json is not valid JSON: {exc}"
                        ),
                    },
                )
        else:
            derived_spec.config_schema = None

        # --- Hard precondition: config contract must be satisfied ---
        if derived_spec.config_schema is None or derived_spec.config_volume is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": (
                        "Repo does not satisfy the robotsix config standard "
                        "(robotsix-standards/docs/config-standard.md).  Every "
                        "deployed service must ship config/config.json + "
                        "config/config.schema.json and declare "
                        "robotsix.deploy.config-target in "
                        "deploy/docker-compose.yml."
                    ),
                },
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
                merged_config = _merge_config(
                    derived_spec.config_schema, base_values, {}
                )
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
                    await config_yaml_store.update_current_and_hash(
                        body.name,
                        merged_config,
                        _canonical_hash(merged_config),
                    )
                except Exception:
                    await config_yaml_store.delete(body.name)
                    raise
            else:
                await config_yaml_store.update_current(body.name, merged_config)

        # --- Seed EnvStore from the repo's env contract ---
        env_store = await _get_env_store(request)
        existing_env = await env_store.get(body.name)
        if not existing_env.env and not existing_env.secret_tokens:
            seeded_env = {k: v for k, v in derived_spec.env.items() if v}
            seeded_secrets = {k: "" for k, v in derived_spec.env.items() if not v}
            if seeded_env or seeded_secrets:
                await env_store.upsert(body.name, seeded_env, seeded_secrets)

    # --- Merge env overrides and secrets ---
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(body.name, comp_cfg.env)
    comp_cfg = comp_cfg.model_copy(update={"env": merged_env})

    # --- Get or create the service record ---
    record = await store.get(body.name)
    if record is None:
        record = ServiceRecord(name=body.name)
        await store.put(record)

    # --- Serialise concurrent deploys ---
    if not await try_acquire_deploy_lock(body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{body.name}'.",
        )

    deploy_image = comp_cfg.image
    try:
        outcome = await backend.deploy(record, comp_cfg, deploy_image)
    except Exception as exc:
        logger.exception("chat deploy %s failed", _sanitize_log(body.name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=body.name,
                action="deploy",
                detail=f"Deploy failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deploy failed: {exc}",
        )
    finally:
        release_deploy_lock(body.name)

    record.state = outcome.state
    record.image = deploy_image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Update the persisted ComponentConfig.image so future dashboard-
    # initiated deploys use the correct image reference.
    if comp_cfg.image != deploy_image:
        comp_cfg.image = deploy_image
        await component_config_store.put(comp_cfg)

    # --- Deploy siblings (best-effort) ---
    deployed_siblings: list[str] = []
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
            try:
                sib_cfg = ComponentConfig(
                    id=sib_name,
                    image=sib.image,
                    container_name=sib.container_name,
                    ports=sib.ports,
                    mounts=sib.mounts,
                    env=sib.env,
                    health_check=sib.health_check,
                    claude_mount=sib.claude_mount,
                    claude_mount_path=sib.claude_mount_path,
                    host_docker_sock=sib.host_docker_sock,
                    named_volumes=[m.host for m in sib.mounts],
                    command=sib.command,
                    entrypoint=sib.entrypoint,
                    tmpfs=sib.tmpfs,
                    mem_limit=sib.mem_limit,
                    user=sib.user,
                )
                sib_outcome = await backend.deploy(sib_record, sib_cfg, sib.image)
                sib_record.state = sib_outcome.state
                sib_record.image = sib.image
                sib_record.deployed_image_digest = sib_outcome.deployed_digest
                sib_record.previous_image_digest = sib_outcome.previous_digest
                await store.put(sib_record)
                deployed_siblings.append(sib_name)
            except Exception as exc:
                logger.warning(
                    "chat deploy sibling '%s' failed: %s",
                    _sanitize_log(sib_name),
                    exc,
                )

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
