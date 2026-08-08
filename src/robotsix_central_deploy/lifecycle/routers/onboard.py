"""Onboard endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...onboard.models import DerivedSpec
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from .._config_utils import _merge_config, _strip_secret_values, inject_deploy_api_key
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    JobRegistry,
    _build_component_config_from_spec,
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_deploy_history_store,
    _get_env_store,
    _get_job_registry,
    _get_registry,
    _get_store,
    _namespace_spec_volumes,
    _validate_config_or_422,
)

from ..models import (
    DeployHistoryEntry,
    DeploySource,
    OnboardJobPhase,
    ServiceRecord,
)
from ..schemas import (
    OnboardConfirmAcceptedResponse,
    OnboardConfirmRequest,
    OnboardJobStatusResponse,
    OnboardPreflightRequest,
    OnboardPreflightResponse,
    PortShift,
)
from ..store import ServiceStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboard"])


# ---------------------------------------------------------------------------
# Private helpers extracted from long route handlers
# ---------------------------------------------------------------------------


async def _deploy_onboard_siblings(
    spec: DerivedSpec,
    store: ServiceStore,
    backend: ExecutionBackend,
    out_records: list[ServiceRecord],
    *,
    allow_chat_access: bool = False,
    api_key: str = "",
) -> None:
    """Deploy all siblings from *spec* (best-effort).

    Appends created records to *out_records* before each deploy so rollback
    can clean up even when a sibling deploy fails partway through.

    Raises RuntimeError if any sibling deploy fails (after attempting all).
    """
    failures: list[str] = []
    for sib in spec.siblings:
        sib_name = f"{spec.name}-{sib.service_key}"
        # Siblings inherit the parent's deploy API key when chat access is enabled.
        sib_env = inject_deploy_api_key(
            sib.env,
            allow_chat_access=allow_chat_access,
            api_key=api_key,
        )
        sib_component_config = ComponentConfig(
            id=sib_name,
            image=sib.image,
            container_name=sib.container_name,
            ports=sib.ports,
            mounts=sib.mounts,
            env=sib_env,
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
        sib_record = ServiceRecord(
            name=sib_name,
            container_name=sib.container_name,
            image=sib.image,
            component_id=spec.name,
        )
        await store.put(sib_record)
        out_records.append(sib_record)

        try:
            sib_outcome = await backend.deploy(
                sib_record, sib_component_config, sib.image
            )
            sib_record.state = sib_outcome.state
            sib_record.image = sib.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
        except Exception as exc:  # noqa: BLE001
            msg = f"deploy onboard sibling '{sib_name}' failed: {exc}"
            logger.warning(msg)
            failures.append(msg)

    if failures:
        raise RuntimeError("; ".join(failures))


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
    named_volumes: list[str] | None = None,
) -> None:
    """Best-effort rollback: remove containers, volumes, config, records, and registry entries."""
    # Remove all deployed containers — primary first, then siblings.
    for rec in [primary_record] + (sibling_records or []):
        try:
            await backend.remove_container(rec)
        except Exception:
            logger.warning(
                "rollback: remove_container %s failed", rec.name, exc_info=True
            )

    # Remove created named volumes so a failed onboard does not leave them behind
    # (which would trip the volume-collision preflight on the next attempt).
    # remove_volume may raise NotImplementedError on DockerBackend (docker_cli) or
    # be absent for already-missing volumes — tolerate everything so rollback
    # never crashes.
    for vol in named_volumes or []:
        try:
            await backend.remove_volume(vol)
        except Exception:
            logger.warning("rollback: remove_volume %s failed", vol, exc_info=True)

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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    lifecycle_config: LifecycleConfig = Depends(_get_config),  # noqa: B008
) -> OnboardPreflightResponse:
    """Fetch and parse a service repo's docker-compose.yml, returning a DerivedSpec.

    The caller reviews the spec before confirming onboarding via `/onboard/confirm`.
    """
    import re

    # Validate name slug
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]*$", req.name):
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"Invalid name '{req.name}': must match ^[a-z0-9][a-z0-9-]*$"
            },
        )

    # Reserved-name guard
    from ...gateway.router import RESERVED_NAMES

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

    # Resolve repo files + parse compose + validate config standard
    # (shared backbone extracted from _resolve_deploy_contract)
    loop = asyncio.get_running_loop()
    from ..deps._compose_resolver import _resolve_compose_backbone

    repo_files, derived_spec = await _resolve_compose_backbone(
        req.git_url, req.name, lifecycle_config, loop
    )

    # Resolve target_disk: explicit request value > config default > empty
    resolved_target_disk = req.target_disk or lifecycle_config.target_disk
    if resolved_target_disk:
        from robotsix_central_deploy.lifecycle._disk_utils import (
            resolve_target_disk,
        )

        try:
            resolved_target_disk = resolve_target_disk(resolved_target_disk)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"Invalid target_disk: {exc}"},
            )
    derived_spec.target_disk = resolved_target_disk

    # Resolve the repo default config per the robotsix-standards config-standard
    # convention (robotsix-standards/docs/config-standard.md). The primary
    # default is config/config.json; fall back to config/config.example.json
    # or the robotsix.deploy.config-template label when absent.
    if repo_files.config_json is not None:
        try:
            parsed_example = json.loads(repo_files.config_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": (f"config/config.json is not valid JSON: {exc}"),
                },
            )
        if not isinstance(parsed_example, dict):
            raise HTTPException(
                status_code=422,
                detail={"error": "config/config.json must be a top-level JSON object"},
            )
        derived_spec.config_example_values = parsed_example
    elif repo_files.config_json_template is not None:
        try:
            parsed_template = json.loads(repo_files.config_json_template)
        except json.JSONDecodeError:
            parsed_template = None
        derived_spec.config_example_values = (
            parsed_template if isinstance(parsed_template, dict) else None
        )
    else:
        derived_spec.config_example_values = None

    # Volume-collision preflight: check that would-be namespaced volume names
    # do not collide with any existing component's named_volumes.
    candidate_volumes: set[str] = {
        f"{req.name}-{vm.host}" for vm in derived_spec.volume_mounts
    } | {f"{req.name}-{vm.host}" for sib in derived_spec.siblings for vm in sib.mounts}
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

    # Port-collision preflight: auto-assign free host ports when defaults collide
    from ...onboard.port_utils import (
        collect_occupied_host_ports,
        find_free_host_port,
    )

    occupied = collect_occupied_host_ports(
        component_config_store, lifecycle_config.port
    )
    port_shifts: list[PortShift] = []

    # Collect all PortMapping objects across primary + siblings.
    # Mutating pm.host in-place updates the DerivedSpec in memory before it is returned.
    for pm in [
        *derived_spec.ports,
        *(pm for sib in derived_spec.siblings for pm in sib.ports),
    ]:
        if pm.host not in occupied:
            occupied.add(pm.host)  # reserve so two incoming ports don't double-assign
            continue
        # Identify the colliding component
        collision_id = ""
        collision_repo_id = ""
        if pm.host == lifecycle_config.port:
            collision_id = "central-deploy"
        else:
            for existing_cfg in component_config_store.all():
                all_ports = list(existing_cfg.ports) + [
                    p for sib in existing_cfg.siblings for p in sib.ports
                ]
                if any(p.host == pm.host for p in all_ports):
                    collision_id = existing_cfg.id
                    collision_repo_id = existing_cfg.repo_id
                    break
        original = pm.host
        pm.host = find_free_host_port(occupied)
        occupied.add(pm.host)
        port_shifts.append(
            PortShift(
                container_port=pm.container,
                protocol=pm.protocol,
                original_host=original,
                assigned_host=pm.host,
                collision_component_id=collision_id,
                collision_repo_id=collision_repo_id,
            )
        )

    return OnboardPreflightResponse(spec=derived_spec, port_shifts=port_shifts)


# ---------------------------------------------------------------------------
# Background deploy job helper
# ---------------------------------------------------------------------------


async def _run_onboard_deploy_job(
    job_id: str,
    spec_name: str,
    spec_image: str,
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
    deploy_history_store: DeployHistoryStore,
    http_client: Any = None,
    settings_store: Any = None,
    port_shifts: list[PortShift] | None = None,
    api_key: str = "",
) -> None:
    """Background task that runs the primary deploy → siblings sequence.

    On any failure, calls ``_rollback_onboard``.
    """
    try:
        # Deploy primary
        if config.health_check is not None and not config.health_check.disable:
            job_registry.update_phase(job_id, OnboardJobPhase.WAITING_HEALTH)
        else:
            job_registry.update_phase(job_id, OnboardJobPhase.DEPLOYING_PRIMARY)

        outcome = await backend.deploy(record, config, config.image)

        record.state = outcome.state
        record.image = config.image
        record.image_revision = outcome.deployed_digest
        record.deployed_image_digest = outcome.deployed_digest
        record.previous_image_digest = outcome.previous_digest
        await store.put(record)

        # Best-effort mill repo registration
        from ...caretaker.mill_client import MillClient

        mill_component_id = ""
        if settings_store is not None:
            mill_component_id = (await settings_store.get()).mill_component_id
        mill_url = MillClient.derive_url_from_registry(
            registry, component_config_store, mill_component_id
        )
        if config.repo_id and mill_url and http_client is not None:
            mc = MillClient(mill_url, http_client)
            ok = await mc.register_repo(config.repo_id, spec.git_url)
            if not ok:
                logger.warning("mill repo registration failed for %s", config.repo_id)

        # File port-collision tickets on affected components' boards
        port_shift_warnings: list[str] = []
        if port_shifts:
            from ...caretaker.models import (
                CaretakerFinding,
                FindingKind,
            )

            for shift in port_shifts:
                filed = False
                if shift.collision_repo_id and mill_url and http_client is not None:
                    finding = CaretakerFinding(
                        component_id=shift.collision_component_id,
                        repo_id=shift.collision_repo_id,
                        kind=FindingKind.PORT_COLLISION,
                        title=(
                            f"Default host port {shift.original_host} collides "
                            f"\u2014 update deploy/docker-compose.yml"
                        ),
                        detail=(
                            f"Component '{shift.collision_component_id}' declares host port "
                            f"{shift.original_host} as a default in its deploy/docker-compose.yml. "
                            f"A new component '{spec_name}' was onboarded and was auto-assigned port "
                            f"{shift.assigned_host} to avoid the collision. "
                            f"Update this component's deploy/docker-compose.yml to use a unique "
                            f"default host port so future onboardings do not collide."
                        ),
                        severity="warning",
                    )
                    mc_ticket = MillClient(mill_url, http_client)
                    filed = await mc_ticket.ingest_finding(finding)
                if not filed and shift.collision_component_id:
                    port_shift_warnings.append(
                        f"Port {shift.original_host} \u2192 {shift.assigned_host}: collided with "
                        f"'{shift.collision_component_id}' \u2014 mill unreachable, "
                        f"update its deploy/docker-compose.yml manually."
                    )

        # Deploy siblings
        job_registry.update_phase(job_id, OnboardJobPhase.DEPLOYING_SIBLINGS)
        sibling_records_created: list[ServiceRecord] = []
        try:
            await _deploy_onboard_siblings(
                spec,
                store,
                backend,
                sibling_records_created,
                allow_chat_access=config.allow_chat_access,
                api_key=api_key,
            )
        except Exception as exc:
            logger.exception("onboard sibling deploy failed for '%s'", spec_name)
            # Capture sibling container logs before rollback.
            sibling_logs: str | None = None
            if sibling_records_created:
                try:
                    sibling_logs = await backend.get_container_logs(
                        sibling_records_created[-1], tail=200
                    )
                except Exception:
                    logger.warning(
                        "onboard %s: failed to capture sibling container logs",
                        spec_name,
                        exc_info=True,
                    )
            # Write a deploy-history entry before rollback so the failed
            # attempt survives cleanup for post-mortem.
            try:
                await deploy_history_store.append(
                    spec_name,
                    DeployHistoryEntry(
                        digest=record.deployed_image_digest or "",
                        image_ref=spec_image,
                        timestamp=time.time(),
                        source=DeploySource.MANUAL,
                        previous_digest=record.previous_image_digest or "",
                    ),
                )
            except Exception:
                logger.warning(
                    "onboard %s: failed to record history entry",
                    spec_name,
                    exc_info=True,
                )
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
                named_volumes=config.named_volumes,
            )
            job_registry.mark_failed(job_id, str(exc), logs=sibling_logs)
            return

        # Success
        job_registry.mark_done(
            job_id,
            name=spec_name,
            image=spec_image,
            state=record.state.value,
            warnings=port_shift_warnings,
        )
    except Exception as exc:
        logger.exception("onboard deploy failed for '%s'", spec_name)
        # Capture container logs before rollback removes the container,
        # so the operator can diagnose startup/healthcheck failures.
        captured_logs: str | None = None
        try:
            captured_logs = await backend.get_container_logs(record, tail=200)
        except Exception:
            logger.warning(
                "onboard %s: failed to capture container logs",
                spec_name,
                exc_info=True,
            )
        # Write a deploy-history entry before rollback so the failed
        # attempt survives cleanup for post-mortem.
        try:
            await deploy_history_store.append(
                spec_name,
                DeployHistoryEntry(
                    digest=record.deployed_image_digest or "",
                    image_ref=spec_image,
                    timestamp=time.time(),
                    source=DeploySource.MANUAL,
                    previous_digest=record.previous_image_digest or "",
                ),
            )
        except Exception:
            logger.warning(
                "onboard %s: failed to record history entry",
                spec_name,
                exc_info=True,
            )
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
            named_volumes=config.named_volumes,
        )
        job_registry.mark_failed(job_id, str(exc), logs=captured_logs)


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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    env_store: EnvStore = Depends(_get_env_store),  # noqa: B008
    job_registry: JobRegistry = Depends(_get_job_registry),  # noqa: B008
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),  # noqa: B008
    lifecycle_config: LifecycleConfig = Depends(_get_config),  # noqa: B008
) -> OnboardConfirmAcceptedResponse:
    """Persist a reviewed DerivedSpec, then schedule the deploy as a background job.

    Returns 202 with a job id so the caller can poll ``GET /onboard/jobs/{job_id}``
    for progress.
    """
    spec = req.spec

    # Namespace volume names so two components from the same image
    # never share Docker named volumes.
    spec = _namespace_spec_volumes(spec, spec.name)

    # Apply target_disk override from confirm request (overrides preflight value).
    if req.target_disk:
        from robotsix_central_deploy.lifecycle._disk_utils import (
            resolve_target_disk,
        )

        try:
            spec.target_disk = resolve_target_disk(req.target_disk)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"Invalid target_disk: {exc}"},
            )

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
    from ...gateway.router import RESERVED_NAMES

    if spec.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Component name '{spec.name}' is reserved"},
        )

    # Build ComponentConfig from the DerivedSpec
    config = _build_component_config_from_spec(spec, git_url=spec.git_url)

    # Derive repo_id from git_url when caretaker is enabled
    settings = await request.app.state.settings_store.get()
    if settings.caretaker_enabled and req.register_with_mill:
        repo_id = spec.git_url.rstrip("/").split("/")[-1].removesuffix(".git")
    else:
        repo_id = ""

    # Mill canonical opt-out: the mill component must never auto-update itself
    caretaker_auto_update = config.id != settings.mill_component_id

    config = config.model_copy(
        update={"repo_id": repo_id, "caretaker_auto_update": caretaker_auto_update}
    )

    # Create the job so the caller can start polling immediately.
    job_id = job_registry.create(spec.name)

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # Reconcile Langfuse auto-projects when the onboarded component has
    # chat access enabled — its config may contain Langfuse key pairs.
    from .._langfuse_config import reconcile_langfuse_after_toggle

    if config.allow_chat_access or config.chat_agent_mutatable:
        await reconcile_langfuse_after_toggle(component_config_store, request)
        logger.info(
            "Onboarded '%s' with chat access — reconciled Langfuse auto-projects",
            config.id,
        )

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
    # config.json to the real config volume so the container starts healthy.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(spec.name, spec.config_schema)
        # Base layer: the repo's config/config.json values ("deploy defaults"),
        # per the robotsix-standards config-standard convention
        # (robotsix-standards/docs/config-standard.md). Secret fields are
        # stripped so example placeholders never inject a secret.
        # Precedence: user form values > deploy defaults > schema default.
        base_values = _strip_secret_values(
            spec.config_schema, spec.config_example_values or {}
        )
        try:
            merged = _merge_config(
                spec.config_schema,
                base_values,
                req.config_values or {},
                prefer_existing_for_unset=True,
            )
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
            except Exception:
                # Roll back the schema we just stored — a component whose
                # config could not be written must not be left half-onboarded.
                await config_yaml_store.delete(spec.name)
                raise

    # Write the fleet-global llmio tier config mapping (all four levels)
    # into the component's config volume before the deploy starts, so
    # robotsix-llmio's TierConfig.for_level() can resolve any capability
    # level from first boot.  Matching deploy_service and put_service_config.
    if config.llmio_tier_level and config.config_volume:
        try:
            settings = await request.app.state.settings_store.get()
            if settings.llmio_tier_config:
                await backend.write_llmio_tier_config_to_volume(
                    config.config_volume, settings.llmio_tier_config
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "onboard %s: could not write llmio tier config to volume %s: %s",
                config.id,
                config.config_volume,
                exc,
            )

    # Create and persist ServiceRecord
    record = ServiceRecord(
        name=spec.name,
        container_name=spec.container_name or spec.name,
        image=spec.image,
        repo_id=repo_id,
    )
    await store.put(record)

    # Inject the deploy API key into the primary config when chat access
    # is enabled — the component can call back to the deploy API immediately.
    api_key = lifecycle_config.api_key.get_secret_value()
    if config.allow_chat_access and api_key:
        config = config.model_copy(
            update={
                "env": inject_deploy_api_key(
                    config.env,
                    allow_chat_access=True,
                    api_key=api_key,
                )
            }
        )

    # Schedule the deploy sequence as a background task.
    asyncio.create_task(
        _run_onboard_deploy_job(
            job_id=job_id,
            spec_name=spec.name,
            spec_image=spec.image,
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
            deploy_history_store=deploy_history_store,
            http_client=request.app.state.http_client
            if hasattr(request.app.state, "http_client")
            else None,
            settings_store=request.app.state.settings_store,
            port_shifts=req.port_shifts,
            api_key=api_key,
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
    job_registry: JobRegistry = Depends(_get_job_registry),  # noqa: B008
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
        phase=cast(OnboardJobPhase, job.phase),
        error=job.error,
        logs=job.logs,
        name=job.name,
        image=job.image,
        state=job.state,
        warnings=job.warnings,
    )
