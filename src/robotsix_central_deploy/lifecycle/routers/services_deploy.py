"""Deploy-related route handlers extracted from services.py."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

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
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deploy_jobs import _run_deploy_job
from ..deploy_lock import (
    get_deploy_lock_info,
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
    DeployHistoryResponse,
    DeployJobPhase,
    DeployRequest,
    ErrorDetail,
    ServiceRecord,
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
        memswap_limit=sib_config.memswap_limit,
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


# Alias: LLM agents recurringly guess "redeploy" for this operation and got
# 404s (2026-09-02: hexarchy 06:40Z, mill 07:46Z — the mill attempt was never
# retried on the right route). Hidden from the OpenAPI schema so the published
# contract keeps a single canonical path.
@router.post(
    "/services/{name}/redeploy",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeployAcceptedResponse,
    include_in_schema=False,
)
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
            "description": (
                "Deploy already in progress for this component, or the mill "
                "has heavy agent stages in flight (retry with force=true)"
            ),
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

    # Guard: recreating the mill container mid-implement aborts hour-scale
    # agent runs whose work is then redone from scratch (the caretaker has
    # deferred for this since #841, but this API path did not — observed
    # 2026-09-03: a chat-driven deploy killed 3 in-flight implement stages).
    # Probes GET /active on the mill; heavy stages → 409 unless force=true.
    # Fails open: an unreachable mill must not block its own deploy.
    settings_store = getattr(request.app.state, "settings_store", None)
    if settings_store is not None and not body.force:
        mill_id = (await settings_store.get()).mill_component_id
        if mill_id and name == mill_id:
            from ...caretaker.mill_client import MillClient

            mill_url = MillClient.derive_url_from_registry(
                registry, component_config_store, mill_id
            )
            if mill_url is not None:
                mill_client = MillClient(mill_url, request.app.state.http_client)
                busy_reason = await mill_client.active_stage_summary()
                if busy_reason is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": (
                                f"Mill is busy — {busy_reason}. Deploying now "
                                "would abort those agent runs and their work "
                                "would be redone from scratch. Wait for the "
                                'stages to finish, or pass {"force": true} '
                                "to deploy anyway."
                            ),
                        },
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
