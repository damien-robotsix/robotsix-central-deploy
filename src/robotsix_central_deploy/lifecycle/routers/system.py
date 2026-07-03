"""Self-update endpoints — update the central-deploy server itself.

``GET /system/update`` reports whether the running server's image is behind
the registry; ``POST /system/update`` launches a one-shot watchtower
container that pulls the new image and recreates the server's own container
(the server cannot safely replace itself from inside).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...registry_check import RegistryChecker
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import _get_backend, _get_config, _get_registry_checker
from ..models import SelfInspect, SelfUpdateStatus, SelfUpdateTriggered

router = APIRouter(prefix="/system", tags=["system"])


async def _inspect_self_or_none(backend: ExecutionBackend) -> SelfInspect | None:
    """``inspect_self`` with backend-unsupported mapped to ``None``."""
    try:
        return await backend.inspect_self()
    except NotImplementedError:
        return None


@router.get("/update", response_model=SelfUpdateStatus)
async def get_self_update_status(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
    checker: RegistryChecker = Depends(_get_registry_checker),
) -> SelfUpdateStatus:
    """Compare the running server image digest against the registry.

    ``supported`` is false when the server is not containerised (or the
    execution backend cannot tell); ``update_available`` is only true when
    both the running and the latest digest resolved and differ.
    """
    self_info = await _inspect_self_or_none(backend)
    if self_info is None:
        return SelfUpdateStatus(supported=False)
    latest = await checker.get_latest_digest(self_info.image_ref) or ""
    update_available = bool(
        latest and self_info.running_digest and latest != self_info.running_digest
    )
    return SelfUpdateStatus(
        supported=True,
        container_name=self_info.container_name,
        image=self_info.image_ref,
        running_digest=self_info.running_digest,
        latest_digest=latest,
        update_available=update_available,
    )


@router.post("/update", response_model=SelfUpdateTriggered, status_code=202)
async def trigger_self_update(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
) -> SelfUpdateTriggered:
    """Launch the one-shot updater; the server restarts shortly after.

    Returns 202 immediately — the dashboard polls ``/health`` /
    ``GET /system/update`` until the recreated server answers with the new
    digest. 503 when self-update is unsupported, 502 when the updater
    container fails to launch.
    """
    self_info = await _inspect_self_or_none(backend)
    if self_info is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "self-update requires the docker_sdk backend running inside a container"
            ),
        )
    try:
        container_id = await backend.trigger_self_update(
            self_info,
            config.self_update_watchtower_image,
            config.docker_socket_url,
            config.self_update_docker_api_version,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SelfUpdateTriggered(updater_container_id=container_id)
