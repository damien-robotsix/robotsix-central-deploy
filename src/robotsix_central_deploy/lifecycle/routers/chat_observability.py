"""Chat agent observability endpoints: logs, status, volume inspection.

Read-only routes gated behind the per-component chat-access checkbox
(``_require_allowed_service``).  Implements Section 5 of the
chat-access-standard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_or_create_record,
    _get_registry_checker,
    _get_store,
    _validate_volume_path,
)
from ..models import ServiceStatus
from ..schemas import VolumeFileResponse
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry_check import RegistryChecker
from ._chat_common import _require_allowed_service, logger

router = APIRouter(tags=["chat"])

#: Maximum bytes returned by chat observability endpoints (~256 KiB).
CHAT_OBSERVABILITY_MAX_BYTES: int = 262_144


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/logs
# ---------------------------------------------------------------------------


@router.get(
    "/chat/services/{name}/logs",
    summary="Recent container logs for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted or not found"},
    },
)
async def chat_service_logs(
    name: str,
    tail: int = Query(100, ge=1, le=10000),
    since: str | None = Query(None, description="ISO 8601 or Unix timestamp"),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> PlainTextResponse:
    """Return recent container logs (bounded tail, no follow).

    Gated by the per-component chat-access checkbox.  Response is
    capped at ~256 KiB to avoid unbounded payloads.
    """
    await _require_allowed_service(name, component_config_store)
    record = await _get_or_create_record(name, store)

    chunks: list[bytes] = []
    total = 0
    async for chunk in backend.stream_logs(
        record, tail=tail, since=since, follow=False
    ):
        chunks.append(chunk)
        total += len(chunk)
        if total >= CHAT_OBSERVABILITY_MAX_BYTES:
            break

    content = b"".join(chunks)
    if len(content) > CHAT_OBSERVABILITY_MAX_BYTES:
        content = content[:CHAT_OBSERVABILITY_MAX_BYTES]

    return PlainTextResponse(
        content=content.decode("utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/status
# ---------------------------------------------------------------------------


@router.get(
    "/chat/services/{name}/status",
    response_model=ServiceStatus,
    summary="Lifecycle status for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted or not found"},
    },
)
async def chat_service_status(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> ServiceStatus:
    """Return machine-readable lifecycle status for an allowlisted service.

    Returns structured JSON (``ServiceStatus``) — no HTML redirect.
    Includes live state from the Docker backend and registry update
    availability when the registry checker is active.
    """
    await _require_allowed_service(name, component_config_store)
    record = await _get_or_create_record(name, store)

    # Refresh live state from backend (best-effort).
    inspect = await backend.status(record)
    changed = (
        inspect.state != record.state
        or inspect.image_revision != record.image_revision
        or inspect.health != record.health
    )
    if changed:
        record.state = inspect.state
        record.image_revision = inspect.image_revision
        record.health = inspect.health
        await store.put(record)

    if (
        inspect.running_digest
        and inspect.running_digest != record.deployed_image_digest
    ):
        record.deployed_image_digest = inspect.running_digest
        await store.put(record)

    # Registry check — update if we have image+digest and checker is available.
    checker: RegistryChecker = _get_registry_checker(request)
    if record.image and record.deployed_image_digest:
        try:
            latest = await checker.get_latest_digest(record.image)
            if latest is not None:
                new_ua = latest != record.deployed_image_digest
                if (
                    record.update_available != new_ua
                    or record.latest_registry_digest != latest
                ):
                    record.update_available = new_ua
                    record.latest_registry_digest = latest
                    await store.put(record)
        except Exception:
            logger.debug(
                "chat status: registry check failed for '%s'",
                name,
                exc_info=True,
            )

    return record.to_status()


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/volumes
# ---------------------------------------------------------------------------


@router.get(
    "/chat/services/{name}/volumes",
    summary="List volumes for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted or not found"},
    },
)
async def chat_service_volumes(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> list[str]:
    """Return the named volumes owned by an allowlisted service.

    Gated by the per-component chat-access checkbox.  Returns an
    empty list when the service has no named volumes.
    """
    await _require_allowed_service(name, component_config_store)
    cfg = component_config_store.get(name)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{name}' not found.",
        )
    return cfg.named_volumes


# ---------------------------------------------------------------------------
# GET /chat/services/{name}/volumes/{vol}/files
# ---------------------------------------------------------------------------


@router.get(
    "/chat/services/{name}/volumes/{vol}/files",
    response_model=VolumeFileResponse,
    summary="Read a file from an allowlisted service's volume",
    responses={
        400: {"description": "Invalid path (traversal or NUL)"},
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service or volume not found"},
        501: {"description": "Volume browsing not supported"},
    },
)
async def chat_volume_file(
    name: str,
    vol: str,
    path: str = Query("", description="File path relative to the volume root"),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> VolumeFileResponse:
    """Return the text content of a file within a service's named volume.

    Read-only — no writes, no deletes.  Path traversal (``..``) and
    absolute-path escapes are rejected.  Responses are capped at
    ~256 KiB (``CHAT_OBSERVABILITY_MAX_BYTES``).  Binary files
    return ``content=null`` and ``binary=True``.
    """
    await _require_allowed_service(name, component_config_store)

    # Verify the volume belongs to this component.
    cfg = component_config_store.get(name)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{name}' not found.",
        )
    if vol not in cfg.named_volumes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Volume '{vol}' is not owned by service '{name}'.",
        )

    rel = _validate_volume_path(path)
    try:
        result = await backend.read_volume_file(vol, rel, CHAT_OBSERVABILITY_MAX_BYTES)
    except NotImplementedError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Volume browsing not supported by this backend",
        )
    return VolumeFileResponse(**result)
