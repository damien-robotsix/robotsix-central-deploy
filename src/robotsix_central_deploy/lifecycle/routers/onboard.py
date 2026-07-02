"""Onboard endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backend import ExecutionBackend
from ..deps import (
    _get_store,
    _get_backend,
    _get_registry,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_env_store,
    _get_job_registry,
    _namespace_spec_volumes,
    _merge_config,
    _validate_config_or_422,
    _canonical_hash,
    JobRegistry,
)
from ..models import ServiceRecord
from ..schemas import (
    OnboardPreflightRequest,
    OnboardPreflightResponse,
    OnboardConfirmRequest,
    OnboardConfirmAcceptedResponse,
    OnboardJobStatusResponse,
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
    backend: ExecutionBackend,
    env_store: EnvStore,
    primary_record: ServiceRecord,
    env_was_seeded: bool,
    sibling_records: list[ServiceRecord] | None = None,
) -> None:
    """Best-effort rollback: remove containers, clean up config, records, and registry entries."""
    # Remove all deployed containers — primary first, then siblings.
    for rec in [primary_record] + (sibling_records or []):
        try:
            await backend.remove_container(rec)
        except Exception:
            logger.warning(
                "rollback: remove_container %s failed", rec.name, exc_info=True
            )

    if sibling_records:
        for sib_rec in sibling_records:
            await store.delete(sib_rec.name)
    await config_yaml_store.delete(name)
    await component_config_store.delete(config_id)
    registry.unregister(config_id)
    await store.delete(name)

    if env_was_seeded:
        await env_store.delete(name)


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
        ParseError,
        parse_compose,
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

    # Parse config/config.schema.json if present
    if repo_files.config_schema_json is not None:
        try:
            derived_spec.config_schema = json.loads(repo_files.config_schema_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"config/config.schema.json is not valid JSON: {exc}"},
            )
    else:
        derived_spec.config_schema = None

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
# Background deploy job helper
# ---------------------------------------------------------------------------


async def _run_onboard_deploy_job(
    job_id: str,
    spec_name: str,
    spec_image: str,
    spec_config_schema: dict[str, Any] | None,
    spec: DerivedSpec,
    config: ComponentConfig,
    record: ServiceRecord,
    store: ServiceStore,
    backend: ExecutionBackend,
    config_yaml_store: ConfigYamlStore,
    component_config_store: ComponentConfigStore,
    registry: ComponentRegistry,
    env_store: EnvStore,
    env_was_seeded: bool,
    job_registry: JobRegistry,
    http_client: Any = None,
    settings_store: Any = None,
) -> None:
    """Background task that runs the primary deploy → siblings sequence.

    On any failure, calls ``_rollback_onboard`` with the exact same
    arguments and ordering as the old synchronous handler.
    """
    try:
        # Deploy primary
        if config.health_check is not None:
            job_registry.update_phase(job_id, "waiting_health")
        else:
            job_registry.update_phase(job_id, "deploying_primary")

        outcome = await backend.deploy(record, config, config.image)

        record.state = outcome.state
        record.image = config.image
        record.image_revision = outcome.deployed_digest
        record.deployed_image_digest = outcome.deployed_digest
        record.previous_image_digest = outcome.previous_digest
        await store.put(record)

        # Best-effort mill repo registration
        if config.repo_id:
            import os

            from ...caretaker.mill_client import MillClient

            mill_url = MillClient.derive_url_from_registry(
                registry, component_config_store
            ) or os.environ.get("MILL_INGEST_URL")
            if mill_url and http_client is not None:
                mc = MillClient(mill_url, http_client)
                ok = await mc.register_repo(config.repo_id, spec.git_url)
                if not ok:
                    logger.warning(
                        "mill repo registration failed for %s", config.repo_id
                    )

        # Deploy siblings
        job_registry.update_phase(job_id, "deploying_siblings")
        sibling_records_created: list[ServiceRecord] = []
        try:
            await _deploy_onboard_siblings(
                spec, store, backend, sibling_records_created
            )
        except Exception as exc:
            logger.exception("onboard sibling deploy failed for '%s'", spec_name)
            await _rollback_onboard(
                spec_name,
                config.id,
                store,
                config_yaml_store,
                component_config_store,
                registry,
                backend=backend,
                env_store=env_store,
                primary_record=record,
                env_was_seeded=env_was_seeded,
                sibling_records=sibling_records_created,
            )
            job_registry.mark_failed(job_id, str(exc))
            return

        # Success
        job_registry.mark_done(
            job_id,
            name=spec_name,
            image=spec_image,
            state=record.state.value,
        )
    except Exception as exc:
        logger.exception("onboard deploy failed for '%s'", spec_name)
        await _rollback_onboard(
            spec_name,
            config.id,
            store,
            config_yaml_store,
            component_config_store,
            registry,
            backend=backend,
            env_store=env_store,
            primary_record=record,
            env_was_seeded=env_was_seeded,
        )
        job_registry.mark_failed(job_id, str(exc))


# ---------------------------------------------------------------------------
# POST /onboard/confirm
# ---------------------------------------------------------------------------


@router.post(
    "/onboard/confirm",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OnboardConfirmAcceptedResponse,
)
async def onboard_confirm(
    req: OnboardConfirmRequest,
    request: Request,
    _: None = Depends(verify_auth),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
    job_registry: JobRegistry = Depends(_get_job_registry),
) -> OnboardConfirmAcceptedResponse:
    """Persist a reviewed DerivedSpec, then schedule the deploy as a background job.

    Returns 202 with a job id so the caller can poll ``GET /onboard/jobs/{job_id}``
    for progress.
    """
    spec = req.spec

    # Namespace volume names so two components from the same image
    # never share Docker named volumes.
    spec = _namespace_spec_volumes(spec, spec.name)

    # Active-job guard: a second confirm for the same component while a
    # job is in flight is rejected with 409.
    if job_registry.has_active_job_for(spec.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": f"onboarding already in progress for component '{spec.name}'"
            },
        )

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

    # Derive repo_id from git_url when caretaker is enabled
    settings = await request.app.state.settings_store.get()
    if settings.caretaker_enabled and req.register_with_mill:
        repo_id = spec.git_url.rstrip("/").split("/")[-1].removesuffix(".git")
    else:
        repo_id = ""

    # Mill canonical opt-out: the mill component must never auto-update itself
    caretaker_auto_update = config.id != "mill"

    config = config.model_copy(
        update={"repo_id": repo_id, "caretaker_auto_update": caretaker_auto_update}
    )

    # Create the job so the caller can start polling immediately.
    job_id = job_registry.create(spec.name)

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # Seed EnvStore from the repo's env contract — first onboard only
    env_was_seeded = False
    existing_env = await env_store.get(spec.name)
    if not existing_env.env and not existing_env.secret_tokens:
        seeded_env = {k: v for k, v in spec.env.items() if v}
        seeded_secrets = {k: "" for k, v in spec.env.items() if not v}
        if seeded_env or seeded_secrets:
            await env_store.upsert(spec.name, seeded_env, seeded_secrets)
            env_was_seeded = True

    # If config schema present, save template + user values and write merged
    # config.yaml to the real config volume so the container starts healthy.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(spec.name, spec.config_schema)
        try:
            merged = _merge_config(spec.config_schema, {}, req.config_values or {})
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": str(exc)},
            )
        # Validate merged result against schema before writing
        _validate_config_or_422(spec.config_schema, merged)
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

    # Create and persist ServiceRecord
    record = ServiceRecord(
        name=spec.name,
        container_name=spec.container_name or spec.name,
        image=spec.image,
        repo_id=repo_id,
    )
    await store.put(record)

    # Schedule the deploy sequence as a background task.
    asyncio.create_task(
        _run_onboard_deploy_job(
            job_id=job_id,
            spec_name=spec.name,
            spec_image=spec.image,
            spec_config_schema=spec.config_schema,
            spec=spec,
            config=config,
            record=record,
            store=store,
            backend=backend,
            config_yaml_store=config_yaml_store,
            component_config_store=component_config_store,
            registry=registry,
            env_store=env_store,
            env_was_seeded=env_was_seeded,
            job_registry=job_registry,
            http_client=request.app.state.http_client
            if hasattr(request.app.state, "http_client")
            else None,
            settings_store=request.app.state.settings_store,
        )
    )

    return OnboardConfirmAcceptedResponse(job_id=job_id, name=spec.name)


# ---------------------------------------------------------------------------
# GET /onboard/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/onboard/jobs/{job_id}", response_model=OnboardJobStatusResponse)
async def onboard_job_status(
    job_id: str,
    _: None = Depends(verify_auth),
    job_registry: JobRegistry = Depends(_get_job_registry),
) -> OnboardJobStatusResponse:
    """Return the current phase of an onboard background deploy job."""
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"unknown job '{job_id}'"},
        )
    return OnboardJobStatusResponse(
        job_id=job.job_id,
        component=job.component,
        phase=job.phase,
        error=job.error,
        name=job.name,
        image=job.image,
        state=job.state,
    )
