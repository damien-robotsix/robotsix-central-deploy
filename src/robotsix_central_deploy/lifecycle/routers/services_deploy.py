"""Deploy-related route handlers extracted from services.py."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.params import Body

from ...registry import ComponentConfig, ServiceConfig
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.traefik_labels import public_url
from .._config_utils import (
    _sanitize_log,
    _write_llmio_tier_config,
    config_document_from_template,
)
from ..auth import verify_auth
from ..backends import ExecutionBackend, collect_protected_image_refs
from ..deploy_lock import (
    get_deploy_lock_info,
    release_deploy_lock,
    set_deploy_lock_job_id,
    try_acquire_deploy_lock,
)
from ..deps import (
    JobRegistry,
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_deploy_history_store,
    _get_env_store,
    _get_job_registry,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
)
from ..models import (
    DeployHistoryEntry,
    DeployHistoryResponse,
    DeployJobPhase,
    DeployRequest,
    DeploySource,
    ErrorDetail,
    RollbackRequest,
    RollbackResponse,
    ServiceRecord,
    ServiceState,
)
from ..schemas import (
    DeployAcceptedResponse,
    DeployJobStatusResponse,
)
from ..store import ServiceStore

logger = logging.getLogger(__name__)


router = APIRouter(tags=["services"])


#: Attempts and spacing for the post-deploy edge probe. Recreating a container
#: takes its route with it until Traefik observes the replacement, so the first
#: probe of a perfectly healthy component can legitimately 404 for a few
#: seconds — see "Redeploying a component briefly 404s it" in docs/edge.md.
_EDGE_PROBE_ATTEMPTS = 6
_EDGE_PROBE_INTERVAL_SECONDS = 5.0


async def _verify_edge_route(config: ComponentConfig, base_domain: str) -> str | None:
    """Check that the edge actually serves *config*; return a warning or None.

    Container health says the process is up. It says nothing about whether the
    edge will carry a request to it, and the two failed independently: a
    component whose registry entry had lost its port deployed healthy for days
    behind a public URL that answered 404, because nothing in the deploy path
    ever looked at that URL.

    ``/health`` is the auth-exempt router, so this needs no credentials. A 404
    is the specific signature of "no router exists"; 401 and 302 both mean the
    edge routed the request and the gate answered, which is success here.

    Advisory only — it returns a message, never raises, and never fails a
    deploy that otherwise succeeded.
    """
    url = public_url(config, base_domain)
    if url is None:
        # No gateway configured, no port, or deliberately not routable. All
        # normal; there is no public URL to hold to account.
        return None

    probe_url = f"{url}/health"
    detail = "no response"
    for attempt in range(_EDGE_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_EDGE_PROBE_INTERVAL_SECONDS)
        try:
            # A plain client, not the shared RetryClient: that one calls
            # raise_for_status, and the status codes this check exists to read
            # are exactly the ones it turns into exceptions.
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(probe_url)
        except Exception as exc:  # noqa: BLE001 — advisory probe, any failure is a warning
            detail = f"{type(exc).__name__}: {exc}"
            continue
        code = response.status_code
        if code != 404 and code < 500:
            return None
        detail = (
            "404 — the edge has no router for this hostname"
            if code == 404
            else f"HTTP {code} — the edge routed the request but got no healthy answer"
        )

    return (
        f"Edge route not verified: GET {probe_url} answered {detail} after "
        f"{_EDGE_PROBE_ATTEMPTS} attempts over "
        f"{int((_EDGE_PROBE_ATTEMPTS - 1) * _EDGE_PROBE_INTERVAL_SECONDS)}s. "
        "The container is healthy, so this is a routing fault, not a component "
        "fault — check that it has a port in its deploy contract and that "
        "Traefik accepted its labels."
    )


# ---------------------------------------------------------------------------
# Fanout helpers
# ---------------------------------------------------------------------------


def _build_sibling_config(
    sib_config: ServiceConfig,
    sib_name: str,
    merged_env: dict[str, str],
) -> ComponentConfig:
    """Build a ComponentConfig for a sibling service from its ServiceConfig and merged env."""
    return ComponentConfig(
        id=sib_name,
        image=sib_config.image,
        container_name=sib_config.container_name,
        ports=sib_config.ports,
        mounts=sib_config.mounts,
        env=merged_env,
        health_check=sib_config.health_check,
        claude_mount=sib_config.claude_mount,
        claude_mount_path=sib_config.claude_mount_path,
        host_docker_sock=sib_config.host_docker_sock,
        named_volumes=[m.host for m in sib_config.mounts],
        command=sib_config.command,
        entrypoint=sib_config.entrypoint,
        tmpfs=sib_config.tmpfs,
        mem_limit=sib_config.mem_limit,
        user=sib_config.user,
    )


async def _fanout_sibling_action(
    name: str,
    store: ServiceStore,
    registry: ComponentRegistry,
    env_store: EnvStore,
    *,
    action: Callable[
        [ServiceConfig, ServiceRecord, str, ComponentConfig], Awaitable[None]
    ],
    action_label: str,
    consumed_scopes: list[str] | None = None,
) -> None:
    """Fan out an action to all siblings of *name* (best-effort per sibling).

    Fetches the fresh config, iterates sibling pairs, resolves merged env,
    builds an effective ComponentConfig, and calls *action* for each sibling.
    Exceptions are caught and logged as warnings so one failing sibling does
    not block the others.
    """
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        # Siblings inherit the parent's consumed-scope resolution.
        if consumed_scopes:
            cred_env = await env_store.resolve_consumed_credentials(
                sib_name, consumed_scopes
            )
            if cred_env:
                merged_env = {**cred_env, **merged_env}
        effective_sib = _build_sibling_config(sib_config, sib_name, merged_env)
        try:
            await action(sib_config, sib_record, sib_name, effective_sib)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s sibling '%s' failed",
                action_label,
                _sanitize_log(sib_name),
            )


# ---------------------------------------------------------------------------
# POST /services/{name}/deploy
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/deploy",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeployAcceptedResponse,
    summary="Deploy a new image version for a service (async)",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service or component config not found",
        },
        409: {
            "model": ErrorDetail,
            "description": "Deploy already in progress for this component",
        },
        503: {"model": ErrorDetail, "description": "Registry not loaded"},
    },
)
async def deploy_service(
    name: str,
    request: Request,
    body: DeployRequest | None = Body(default=None),  # type: ignore[assignment]  # noqa: B008
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),  # noqa: B008
    job_registry: JobRegistry = Depends(_get_job_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> DeployAcceptedResponse:
    """Queue a deploy for a service and return immediately with a job id.

    Optionally accepts a specific image reference; defaults to the component's
    configured image.  The deploy runs as a background job — the caller polls
    ``GET /services/deploy-jobs/{job_id}`` for progress.

    Returns 202 with a ``job_id`` so the UI can poll progress.  Raises 404 if
    the service or component config is not found, 409 when a deploy is already
    in progress for the component and no job record exists (e.g. caretaker
    deploy), and 503 when the registry checker is not loaded.
    """
    if body is None:
        body = DeployRequest()

    record = await _get_or_create_record(name, store)

    # Guard: central-deploy cannot deploy itself through this path.
    # Self-updates must use the detached updater via POST /system/update —
    # the management plane cannot safely tear itself down from inside.
    if name == "central-deploy":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    "Cannot deploy central-deploy through this endpoint. "
                    "The management plane cannot safely replace itself from inside. "
                    "Use POST /system/update to trigger a detached self-update."
                ),
            },
        )

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot deploy",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    # Resolve credentials shared by other components via scope tags.
    if config.consumed_scopes:
        cred_env = await env_store.resolve_consumed_credentials(
            name, config.consumed_scopes
        )
        if cred_env:
            merged_env = {**cred_env, **merged_env}
    # Inject the deploy API key when chat access is enabled so the
    # component can call back to the deploy API without manual setup.
    config = config.model_copy(update={"env": merged_env})

    image_ref = body.image or config.image

    # Apply per-deploy target_disk override and persist it.
    if body.target_disk:
        from robotsix_central_deploy.lifecycle._disk_utils import (
            resolve_target_disk,
        )

        try:
            resolved = resolve_target_disk(body.target_disk)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"Invalid target_disk: {exc}"},
            )
        config = config.model_copy(update={"target_disk": resolved})
        await component_config_store.put(config)

    # If an API-initiated deploy job is already active for this component,
    # return its job_id so the caller can poll the existing deploy.
    existing_job_id = job_registry.active_deploy_job_id_for(name)
    if existing_job_id is not None:
        return DeployAcceptedResponse(job_id=existing_job_id, name=name)

    # Serialise concurrent deploys of the same component (operator + caretaker).
    if not await try_acquire_deploy_lock(name, source="manual"):
        lock_info = get_deploy_lock_info(name)
        lock_source = lock_info.get("source", "unknown") if lock_info else "unknown"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    f"Deploy already in progress for '{name}'"
                    f" (started by {lock_source})"
                ),
                "detail": lock_info,
            },
        )

    job_id = job_registry.create_deploy(name)
    set_deploy_lock_job_id(name, job_id)

    # Schedule the deploy sequence as a background task.
    settings_store = getattr(request.app.state, "settings_store", None)
    lifecycle_config = await _get_config(request)
    asyncio.create_task(
        _run_deploy_job(
            job_id=job_id,
            name=name,
            image_ref=image_ref,
            record=record,
            config=config,
            store=store,
            backend=backend,
            registry=registry,
            env_store=env_store,
            deploy_history_store=deploy_history_store,
            config_yaml_store=config_yaml_store,
            job_registry=job_registry,
            settings_store=settings_store,
            gateway_base_domain=lifecycle_config.gateway_base_domain,
        )
    )

    return DeployAcceptedResponse(job_id=job_id, name=name)


# ---------------------------------------------------------------------------
# Background deploy job runner
# ---------------------------------------------------------------------------


async def _run_deploy_job(
    job_id: str,
    name: str,
    image_ref: str,
    record: ServiceRecord,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
    deploy_history_store: DeployHistoryStore,
    config_yaml_store: ConfigYamlStore,
    job_registry: JobRegistry,
    settings_store: Any = None,
    gateway_base_domain: str = "",
) -> None:
    """Background task that runs the full deploy sequence and updates the job.

    On failure the component record is marked FAILED and the job is marked
    failed so the polling endpoint surfaces the error.
    """
    try:
        # Write merged config.json into the config volume before starting.
        if config.config_volume:
            # The component's own config file is the source of truth, so read
            # it first — both the drift guard and the seed decision below
            # depend on knowing whether it exists.
            try:
                live_dict = await backend.read_config_from_volume(config.config_volume)
            except Exception:  # noqa: BLE001
                live_dict = {}

            # Seed only. The component owns its config; the deploy plane must
            # never write over a config file that already exists.
            #
            # This used to push the stored `current` onto the volume on every
            # deploy, which made the deploy-side copy authoritative in
            # practice: a stale entry silently reverted the component's own
            # settings, and fixing the volume by hand lasted until the next
            # deploy. On 2026-08-07 that overwrote chat's Langfuse credentials
            # with an empty pre-migration block and removed it from fleet-wide
            # Langfuse discovery, with nothing erroring anywhere.
            #
            # A component with no config file yet still needs one, so the
            # shipped template is written when — and only when — the volume is
            # empty.
            if not live_dict:
                template_cfg = await config_yaml_store.get_template(name)
                if template_cfg:
                    # The stored template is a JSON Schema, not a config
                    # document — see config_document_from_template for what
                    # writing it verbatim costs.
                    seed_cfg = config_document_from_template(template_cfg)
                    try:
                        await backend.write_config_to_volume(
                            config.config_volume, seed_cfg
                        )
                        logger.info(
                            "deploy %s: seeded config.json into empty volume %s",
                            _sanitize_log(name),
                            _sanitize_log(config.config_volume),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "deploy %s: could not seed config.json into volume %s: %s",
                            _sanitize_log(name),
                            _sanitize_log(config.config_volume),
                            exc,
                        )
                        # non-fatal: the component may ship its own default

        # Write the fleet-global llmio tier config mapping (all four levels)
        # into the component's config volume so robotsix-llmio's
        # TierConfig.for_level() can resolve any capability level.
        await _write_llmio_tier_config(
            backend, config, settings_store, name, log_context="deploy"
        )

        # Deploy — update job phase for health-wait visibility.
        if config.health_check is not None and not config.health_check.disable:
            job_registry.update_phase(job_id, DeployJobPhase.WAITING_HEALTH)

        outcome = await backend.deploy(record, config, image_ref)

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
                    source=DeploySource.MANUAL,
                    previous_digest=outcome.previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "deploy %s: failed to record history entry",
                _sanitize_log(name),
                exc_info=True,
            )

        # Deploy siblings
        job_registry.update_phase(job_id, DeployJobPhase.DEPLOYING_SIBLINGS)

        async def _do_deploy_sibling(
            sib_config: ServiceConfig,
            sib_record: ServiceRecord,
            sib_name: str,
            effective_sib: ComponentConfig,
        ) -> None:
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
                        source=DeploySource.MANUAL,
                        previous_digest=sib_outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "deploy sibling '%s': failed to record history",
                    _sanitize_log(sib_name),
                    exc_info=True,
                )

        await _fanout_sibling_action(
            name,
            store,
            registry,
            env_store,
            action=_do_deploy_sibling,
            action_label="deploy",
            consumed_scopes=config.consumed_scopes,
        )

        # Auto-prune dangling images left behind by the update (opt-in setting);
        # rollback targets recorded in the store are protected. Entirely
        # best-effort: a settings-store or prune failure must never fail the
        # deploy that already succeeded.
        try:
            settings = (
                await settings_store.get() if settings_store is not None else None
            )
            if settings is not None and settings.image_auto_prune:
                protected = await collect_protected_image_refs(store)
                result = await backend.prune_images(protected)
                reclaimed = result.space_reclaimed_bytes
                if reclaimed:
                    logger.info(
                        "deploy %s: image auto-prune reclaimed %d bytes",
                        _sanitize_log(name),
                        reclaimed,
                    )
        except Exception:
            logger.warning(
                "deploy %s: image auto-prune failed",
                _sanitize_log(name),
                exc_info=True,
            )

        # Verify the edge before calling the deploy done. This is the last
        # step because it needs the replacement container to be up and its
        # labels observed; running it earlier would only measure the recreate
        # window.
        deploy_warnings = list(outcome.warnings)
        try:
            edge_warning = await _verify_edge_route(config, gateway_base_domain)
        except Exception:
            # Never fail a deploy on the check itself.
            logger.warning(
                "deploy %s: edge route check could not run",
                _sanitize_log(name),
                exc_info=True,
            )
            edge_warning = None
        if edge_warning:
            logger.warning("deploy %s: %s", _sanitize_log(name), edge_warning)
            deploy_warnings.append(edge_warning)

        job_registry.mark_done(
            job_id,
            name=name,
            image=image_ref,
            state=record.state.value,
            warnings=deploy_warnings,
        )
    except Exception as exc:
        logger.exception("deploy %s failed", _sanitize_log(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        # Capture container logs so the operator can diagnose the failure.
        captured_logs: str | None = None
        try:
            captured_logs = await backend.get_container_logs(record, tail=200)
        except Exception:
            logger.warning(
                "deploy %s: failed to capture container logs",
                _sanitize_log(name),
                exc_info=True,
            )
        job_registry.mark_failed(job_id, str(exc), logs=captured_logs)
    finally:
        release_deploy_lock(name)


# ---------------------------------------------------------------------------
# GET /services/deploy-jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get(
    "/services/deploy-jobs/{job_id}",
    response_model=DeployJobStatusResponse,
    summary="Poll the status of a background deploy job",
    responses={
        404: {"model": ErrorDetail, "description": "Job not found"},
    },
)
async def deploy_job_status(
    job_id: str,
    _: None = Depends(verify_auth),
    job_registry: JobRegistry = Depends(_get_job_registry),  # noqa: B008
) -> DeployJobStatusResponse:
    """Return the current phase of a background deploy job."""
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown deploy job '{job_id}'",
        )
    return DeployJobStatusResponse(
        job_id=job.job_id,
        component=job.component,
        phase=cast(DeployJobPhase, job.phase),
        error=job.error,
        logs=job.logs,
        name=job.name,
        image=job.image,
        state=job.state,
        warnings=job.warnings,
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
    body: RollbackRequest | None = Body(default=None),  # type: ignore[assignment]  # noqa: B008
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),  # noqa: B008
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

    # Guard: central-deploy cannot rollback itself through this path.
    # Self-updates must use the detached updater via POST /system/update.
    if name == "central-deploy":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    "Cannot rollback central-deploy through this endpoint. "
                    "The management plane cannot safely replace itself from inside. "
                    "Use POST /system/update to trigger a detached self-update."
                ),
            },
        )

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot rollback",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    # Resolve credentials shared by other components via scope tags.
    if config.consumed_scopes:
        cred_env = await env_store.resolve_consumed_credentials(
            name, config.consumed_scopes
        )
        if cred_env:
            merged_env = {**cred_env, **merged_env}
    # Inject the deploy API key when chat access is enabled.
    config = config.model_copy(update={"env": merged_env})

    if body is None:
        body = RollbackRequest()

    async def _do_rollback_sibling(
        sib_config: ServiceConfig,
        sib_record: ServiceRecord,
        sib_name: str,
        effective_sib: ComponentConfig,
    ) -> None:
        if not sib_record.previous_image_digest:
            logger.warning(
                "rollback sibling '%s-%s': no prior digest — skipping",
                _sanitize_log(name),
                _sanitize_log(sib_config.service_key),
            )
            return
        sib_outcome = await backend.rollback(sib_record, effective_sib)
        old_dep_sib = sib_record.deployed_image_digest
        old_prev_sib = sib_record.previous_image_digest
        sib_record.state = sib_outcome.state
        sib_record.deployed_image_digest = old_prev_sib
        sib_record.previous_image_digest = old_dep_sib
        sib_record.image_revision = old_prev_sib
        await store.put(sib_record)

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
            logger.exception("rollback %s failed", _sanitize_log(name))
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
                    source=DeploySource.ROLLBACK,
                    previous_digest=deploy_outcome.previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "rollback %s: failed to record history entry",
                _sanitize_log(name),
                exc_info=True,
            )

        # Fan out siblings (one-step; current behaviour)
        await _fanout_sibling_action(
            name,
            store,
            registry,
            env_store,
            action=_do_rollback_sibling,
            action_label="rollback",
            consumed_scopes=config.consumed_scopes,
        )

        return RollbackResponse(
            name=name,
            rolled_back_to_digest=deploy_outcome.deployed_digest,
            current_state=record.state,
            warnings=deploy_outcome.warnings,
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
        logger.exception("rollback %s failed", _sanitize_log(name))
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
    await _fanout_sibling_action(
        name,
        store,
        registry,
        env_store,
        action=_do_rollback_sibling,
        action_label="rollback",
        consumed_scopes=config.consumed_scopes,
    )

    return RollbackResponse(
        name=name,
        rolled_back_to_digest=old_previous,
        current_state=record.state,
        warnings=outcome.warnings,
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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> DeployHistoryResponse:
    """Return the deploy history for a service, most-recent-first.

    Returns 200 with an empty ``entries`` list when no history is recorded.
    Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    entries = await deploy_history_store.list(name)
    return DeployHistoryResponse(name=name, entries=entries)
