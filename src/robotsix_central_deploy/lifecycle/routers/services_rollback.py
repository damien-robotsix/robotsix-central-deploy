"""Rollback route handler extracted from services_deploy.py."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.params import Body

from ...registry import ComponentConfig, ServiceConfig
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_deploy_history_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_store,
)
from ..models import (
    DeployHistoryEntry,
    DeployOutcome,
    DeploySource,
    ErrorDetail,
    RollbackOutcome,
    RollbackRequest,
    RollbackResponse,
    ServiceRecord,
    ServiceState,
)
from ..store import ServiceStore
from .services_deploy import _fanout_sibling_action

logger = logging.getLogger(__name__)


router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# POST /services/{name}/rollback
# ---------------------------------------------------------------------------


async def _execute_rollback_staging(
    name: str,
    record: ServiceRecord,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    deploy_history_store: DeployHistoryStore,
    *,
    image_ref: str | None = None,
) -> tuple[str, ServiceState, list[str]]:
    """Run one rollback staging step: backend op, digest swap, persist, history.

    Shared by both ``rollback_service`` branches and the sibling fan-out so
    the rollback bookkeeping lives in exactly one place (mirroring how
    ``_run_deploy_job`` centralizes deploy staging).

    *image_ref*: digest-pinned repo ref for a multi-entry (target-digest)
    rollback — the backend deploys that ref and the outcome's deployed /
    previous digests are recorded verbatim, and a ``DeployHistoryEntry`` with
    ``source=ROLLBACK`` is appended (best-effort — a history-store failure
    never fails the rollback). When None, the original one-step swap applies:
    the backend rolls the container back to ``record.previous_image_digest``
    and the two stored digests are exchanged in place.

    Returns ``(rolled_back_to_digest, current_state, warnings)``. On backend
    failure the record is marked FAILED and an HTTP 500 is raised.
    """
    outcome: DeployOutcome | RollbackOutcome
    try:
        if image_ref is not None:
            deploy_outcome = await backend.deploy(record, config, image_ref)
            deployed_digest = deploy_outcome.deployed_digest
            previous_digest = deploy_outcome.previous_digest
            outcome = deploy_outcome
        else:
            # One-step: snapshot the current digests, then swap them in place.
            deployed_digest = record.previous_image_digest
            previous_digest = record.deployed_image_digest
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

    record.state = outcome.state
    record.deployed_image_digest = deployed_digest
    record.previous_image_digest = previous_digest
    record.image_revision = deployed_digest
    record.last_error = ""
    await store.put(record)

    if image_ref is not None:
        try:
            await deploy_history_store.append(
                name,
                DeployHistoryEntry(
                    digest=deployed_digest,
                    image_ref=image_ref,
                    timestamp=time.time(),
                    source=DeploySource.ROLLBACK,
                    previous_digest=previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "rollback %s: failed to record history entry",
                _sanitize_log(name),
                exc_info=True,
            )

    return deployed_digest, record.state, outcome.warnings


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
        await _execute_rollback_staging(
            sib_name,
            sib_record,
            effective_sib,
            store,
            backend,
            deploy_history_store,
        )

    if body.digest is not None:
        # -- Target-digest rollback (multi-entry history) ------------------
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
            prefix, image_name = bare.rsplit("/", 1)
            image_name = (
                image_name.rsplit(":", 1)[0] if ":" in image_name else image_name
            )
            repo = f"{prefix}/{image_name}"
        else:
            repo = bare.rsplit(":", 1)[0] if ":" in bare else bare
        image_ref = f"{repo}@{target_digest}"

        rolled_back_to, state, warnings = await _execute_rollback_staging(
            name,
            record,
            config,
            store,
            backend,
            deploy_history_store,
            image_ref=image_ref,
        )
    else:
        # -- Original one-step rollback -----------------------------------
        if not record.previous_image_digest:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No prior image digest recorded for '{name}' — run a deploy first",
            )
        rolled_back_to, state, warnings = await _execute_rollback_staging(
            name,
            record,
            config,
            store,
            backend,
            deploy_history_store,
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
        rolled_back_to_digest=rolled_back_to,
        current_state=state,
        warnings=warnings,
    )
