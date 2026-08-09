"""Chat agent observability endpoints: logs, status, volume inspection.

Read-only routes gated behind the per-component chat-access checkbox
(``_require_allowed_service``).  Implements Section 5 of the
chat-access-standard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from ...registry.config_store import ComponentConfigStore
from ...registry_check import RegistryChecker
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
from ._chat_common import _require_allowed_service
from ._status_refresh import refresh_record_status

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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> PlainTextResponse:
    """Return recent container logs (bounded tail, no follow).

    Gated by the per-component chat-access checkbox.  Response is
    capped at ~256 KiB to avoid unbounded payloads.
    """
    await _require_allowed_service(name, component_config_store, action="access")
    record = await _get_or_create_record(name, store)

    chunks: list[bytes] = []
    total = 0
    async for chunk in backend.stream_logs(
        record, tail=tail, since=since, follow=False
    ):
        remaining = CHAT_OBSERVABILITY_MAX_BYTES - total
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        total += len(chunk)

    content = b"".join(chunks)

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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ServiceStatus:
    """Return machine-readable lifecycle status for an allowlisted service.

    Returns structured JSON (``ServiceStatus``) — no HTML redirect.
    Includes live state from the Docker backend and registry update
    availability when the registry checker is active.
    """
    await _require_allowed_service(name, component_config_store, action="access")
    record = await _get_or_create_record(name, store)
    checker: RegistryChecker = _get_registry_checker(request)
    await refresh_record_status(record, backend, store, checker)
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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> list[str]:
    """Return the named volumes owned by an allowlisted service.

    Gated by the per-component chat-access checkbox.  Returns an
    empty list when the service has no named volumes.
    """
    await _require_allowed_service(name, component_config_store, action="access")
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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> VolumeFileResponse:
    """Return the text content of a file within a service's named volume.

    Read-only — no writes, no deletes.  Path traversal (``..``) and
    absolute-path escapes are rejected.  Responses are capped at
    ~256 KiB (``CHAT_OBSERVABILITY_MAX_BYTES``).  Binary files
    return ``content=null`` and ``binary=True``.
    """
    await _require_allowed_service(name, component_config_store, action="access")

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
    except IsADirectoryError:
        # Reading a directory used to answer 200 with an empty 4096-byte
        # body — a blank file, as far as the caller could tell.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{rel or '/'}' is a directory, not a file.",
        )
    return VolumeFileResponse(**result)
