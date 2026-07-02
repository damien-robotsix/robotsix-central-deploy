"""Onboard endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_auth
from ..backend import ExecutionBackend
from ..deps import (
    _get_store,
    _get_backend,
    _get_registry,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_env_store,
    _namespace_spec_volumes,
    _merge_config,
    _annotate_secret_sentinels,
    _canonical_hash,
)
from ..models import ServiceRecord
from ..schemas import (
    OnboardPreflightRequest,
    OnboardPreflightResponse,
    OnboardConfirmRequest,
    OnboardConfirmResponse,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig, ServiceConfig
from ...onboard.models import DerivedSpec  # noqa: TCH001

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboard"])


# ---------------------------------------------------------------------------
# Private helpers extracted from long route handlers
# ---------------------------------------------------------------------------


async def _deploy_onboard_siblings(
    spec: "DerivedSpec",
    store: ServiceStore,
    backend: ExecutionBackend,
    out_records: list[ServiceRecord],
) -> None:
    """Deploy all siblings from *spec* (best-effort).

    Appends created records to *out_records* before each deploy so rollback
    can clean up even when a sibling deploy fails partway through.
    """
    for sib in spec.siblings:
        sib_name = f"{spec.name}-{sib.service_key}"
        sib_component_config = ComponentConfig(
            id=sib_name,
            image=sib.image,
            container_name=sib.container_name,
            ports=sib.ports,
            mounts=sib.volume_mounts,
            env=sib.env,
            health_check=sib.health_check,
            claude_mount=sib.claude_mount,
            host_docker_sock=sib.host_docker_sock,
            named_volumes=[m.host for m in sib.volume_mounts],
            command=sib.command,
            entrypoint=sib.entrypoint,
        )
        sib_record = ServiceRecord(
            name=sib_name,
            container_name=sib.container_name,
            image=sib.image,
            component_id=spec.name,
        )
        await store.put(sib_record)
        out_records.append(sib_record)

        sib_outcome = await backend.deploy(sib_record, sib_component_config, sib.image)
        sib_record.state = sib_outcome.state
        sib_record.image = sib.image
        sib_record.deployed_image_digest = sib_outcome.deployed_digest
        sib_record.previous_image_digest = sib_outcome.previous_digest
        await store.put(sib_record)


async def _rollback_onboard(
    name: str,
    config_id: str,
    store: ServiceStore,
    config_yaml_store: ConfigYamlStore,
    component_config_store: ComponentConfigStore,
    registry: ComponentRegistry,
    sibling_names: list[str] | None = None,
) -> None:
    """Best-effort rollback: clean up config, records, and registry entries."""
    if sibling_names:
        for sib_name in sibling_names:
            await store.delete(sib_name)
    await config_yaml_store.delete(name)
    await component_config_store.delete(config_id)
    registry.unregister(config_id)
    await store.delete(name)


# ---------------------------------------------------------------------------
# POST /onboard/preflight
# ---------------------------------------------------------------------------


@router.post("/onboard/preflight", response_model=OnboardPreflightResponse)
async def onboard_preflight(
    req: OnboardPreflightRequest,
    _: None = Depends(verify_auth),
    store: ServiceStore = Depends(_get_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
) -> OnboardPreflightResponse:
    """Fetch and parse a service repo's docker-compose.yml, returning a DerivedSpec.

    The caller reviews the spec before confirming onboarding via `/onboard/confirm`.
    """
    import re

    from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_repo_files
    from robotsix_central_deploy.onboard.parser import (
        ConfigParseError,
        ParseError,
        parse_compose,
        parse_config_yaml,
    )

    # Validate name slug
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]*$", req.name):
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"Invalid name '{req.name}': must match ^[a-z0-9][a-z0-9-]*$"
            },
        )

    # Reserved-name guard
    from ...gateway.router import RESERVED_NAMES  # noqa: PLC0415

    if req.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Component name '{req.name}' is reserved"},
        )

    # Check for duplicate
    existing = await store.get(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"component '{req.name}' already exists"},
        )

    # Fetch repo files (git clone is blocking → run in executor)
    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None,
            fetch_repo_files,
            req.git_url,
        )
    except FetchError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})

    # Parse compose
    try:
        derived_spec = parse_compose(repo_files.compose_bytes, req.name, req.git_url)
    except ParseError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": e.violations},
        )

    # Parse config/config.yaml if present; fall back to config template
    _config_bytes = (
        repo_files.config_yaml
        if repo_files.config_yaml is not None
        else repo_files.config_yaml_template
    )
    if _config_bytes is not None:
        try:
            derived_spec.config_schema = _annotate_secret_sentinels(
                parse_config_yaml(_config_bytes)
            )  # type: ignore[assignment]
        except ConfigParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Preflight gate: config (or template) present but no config-target label
    if derived_spec.config_schema is not None and derived_spec.config_volume is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": (
                    "repo has a config file or template but no service declares "
                    "`robotsix.deploy.config-target` — add the label to "
                    "deploy/docker-compose.yml pointing to the full in-container "
                    "path of the config file (e.g. /home/mailbot/config/config.yaml)"
                ),
            },
        )

    # Preflight gate: config-target label declared but no config file/template found
    if derived_spec.config_volume is not None and derived_spec.config_schema is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": (
                    "config-target label is set but no config file or template was found — "
                    "commit config/config.example.yaml to the repo or set "
                    "robotsix.deploy.config-template to a valid in-repo template path"
                ),
            },
        )

    # Volume-collision preflight: check that would-be namespaced volume names
    # do not collide with any existing component's named_volumes.
    candidate_volumes: set[str] = {
        f"{req.name}-{vm.host}" for vm in derived_spec.volume_mounts
    } | {
        f"{req.name}-{vm.host}"
        for sib in derived_spec.siblings
        for vm in sib.volume_mounts
    }
    if candidate_volumes:
        collisions: list[str] = []
        for existing_cfg in component_config_store.all():  # synchronous
            for vol in sorted(candidate_volumes & set(existing_cfg.named_volumes)):
                collisions.append(
                    f"'{vol}' is already owned by component '{existing_cfg.id}'"
                )
        if collisions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "onboarding would create volume name collision(s) with existing component(s)",
                    "collisions": collisions,
                },
            )

    return OnboardPreflightResponse(spec=derived_spec)


# ---------------------------------------------------------------------------
# POST /onboard/confirm
# ---------------------------------------------------------------------------


@router.post("/onboard/confirm", response_model=OnboardConfirmResponse)
async def onboard_confirm(
    req: OnboardConfirmRequest,
    _: None = Depends(verify_auth),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
) -> OnboardConfirmResponse:
    """Persist a reviewed DerivedSpec, deploy the container, and register the component."""
    spec = req.spec

    # Namespace volume names so two components from the same image
    # never share Docker named volumes.
    spec = _namespace_spec_volumes(spec, spec.name)

    # Race-condition guard: re-check name not already in store
    existing = await store.get(spec.name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"component '{spec.name}' already exists"},
        )

    # Reserved-name guard: don't allow names that shadow API routes
    from ...gateway.router import RESERVED_NAMES  # noqa: PLC0415

    if spec.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Component name '{spec.name}' is reserved"},
        )

    # Build ComponentConfig from the DerivedSpec
    config = ComponentConfig(
        id=spec.name,
        image=spec.image,
        container_name=spec.container_name or spec.name,
        ports=spec.ports,
        mounts=spec.volume_mounts,
        env=spec.env,
        health_check=spec.health_check,
        command=spec.command,
        entrypoint=spec.entrypoint,
        claude_mount=spec.claude_mount,
        host_docker_sock=spec.host_docker_sock,
        named_volumes=[m.host for m in spec.volume_mounts]
        + [m.host for sib in spec.siblings for m in sib.volume_mounts],
        stateful_volumes=spec.stateful_volumes,
        siblings=[
            ServiceConfig(
                service_key=sib.service_key,
                container_name=sib.container_name,
                image=sib.image,
                ports=sib.ports,
                mounts=sib.volume_mounts,
                env=sib.env,
                claude_mount=sib.claude_mount,
                host_docker_sock=sib.host_docker_sock,
                health_check=sib.health_check,
                command=sib.command,
                entrypoint=sib.entrypoint,
            )
            for sib in spec.siblings
        ],
        git_url=spec.git_url,
        has_config_yaml=(spec.config_schema is not None),
    )
    # Wire the real config volume name (resolved by parser from the label)
    config.config_volume = spec.config_volume  # None if no config-target label
    config.config_assist_command = spec.config_assist_command
    config.config_assist_seeds = spec.config_assist_seeds

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # Seed EnvStore from the repo's env contract — first onboard only
    existing_env = await env_store.get(spec.name)
    if not existing_env.env and not existing_env.secret_tokens:
        seeded_env = {k: v for k, v in spec.env.items() if v}
        seeded_secrets = {k: "" for k, v in spec.env.items() if not v}
        if seeded_env or seeded_secrets:
            await env_store.upsert(spec.name, seeded_env, seeded_secrets)

    # If config schema present, save template + user values and write merged
    # config.yaml to the real config volume so the container starts healthy.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(spec.name, spec.config_schema)
        merged = _merge_config(spec.config_schema, {}, req.config_values or {})
        if spec.config_volume is not None:
            try:
                await backend.write_config_to_volume(spec.config_volume, merged)
                await config_yaml_store.update_current_and_hash(
                    spec.name, merged, _canonical_hash(merged)
                )
            except Exception:
                await config_yaml_store.delete(spec.name)
                raise
        else:
            await config_yaml_store.update_current(spec.name, merged)
        # Do NOT add a synthetic "{name}-config" volume — the real volume
        # is already in config.named_volumes (it came from spec.volume_mounts).

    # Create and persist ServiceRecord
    record = ServiceRecord(
        name=spec.name,
        container_name=spec.container_name or spec.name,
        image=spec.image,
    )
    await store.put(record)

    # Deploy primary
    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("onboard deploy failed for '%s'", spec.name)
        await _rollback_onboard(
            spec.name,
            config.id,
            store,
            config_yaml_store,
            component_config_store,
            registry,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(exc)},
        )

    # Update primary record state from outcome
    record.state = outcome.state
    record.image = config.image
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Deploy siblings
    sibling_records_created: list[ServiceRecord] = []
    try:
        await _deploy_onboard_siblings(spec, store, backend, sibling_records_created)
    except Exception as exc:
        logger.exception("onboard sibling deploy failed for '%s'", spec.name)
        await _rollback_onboard(
            spec.name,
            config.id,
            store,
            config_yaml_store,
            component_config_store,
            registry,
            sibling_names=[sr.name for sr in sibling_records_created],
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(exc)},
        )

    return OnboardConfirmResponse(
        name=spec.name,
        image=spec.image,
        state=record.state.value,
    )
