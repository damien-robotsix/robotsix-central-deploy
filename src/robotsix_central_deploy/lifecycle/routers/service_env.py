"""Service environment variable endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_auth
from ..deps import (
    _get_component_config_store,
    _get_env_store,
    _get_or_create_record,
    _get_store,
)
from ..models import ErrorDetail
from ..schemas import EnvResponse, EnvSyncResponse, EnvUpdate
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.env_store import EnvStore

logger = logging.getLogger(__name__)
router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# GET /services/{name}/env
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/env",
    response_model=EnvResponse,
    summary="Get stored environment variables and secret keys for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_env(
    name: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> EnvResponse:
    """Return stored environment variables and masked secret keys for a service.

    Secret values are never exposed — only the key names are returned, each
    masked as ``"***"``. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    config = await env_store.get(name)
    secrets_masked = {key: "***" for key in config.secret_tokens}
    return EnvResponse(env=config.env, secrets=secrets_masked)


# ---------------------------------------------------------------------------
# PUT /services/{name}/env
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/env",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upsert environment variables and secrets for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def put_service_env(
    name: str,
    body: EnvUpdate,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    """Create or update environment variables and secrets for a service.

    Returns 204 No Content on success. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    await env_store.upsert(name, body.env, body.secrets)


# ---------------------------------------------------------------------------
# POST /services/{name}/env/sync-keys
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/env/sync-keys",
    response_model=EnvSyncResponse,
    summary="Add env keys newly declared by the repo's compose contract",
    responses={
        400: {"model": ErrorDetail, "description": "Component has no git_url"},
        404: {"model": ErrorDetail, "description": "Component not found"},
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch or compose parse failed",
        },
    },
)
async def sync_env_keys(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> EnvSyncResponse:
    """Re-read the repo's compose env contract and seed newly declared keys.

    The env key set belongs to the repo's contract, not the operator: keys
    with a default value are added as plain env entries, keys declared empty
    are added as secret slots — mirroring onboard seeding. Existing values
    are never modified, and keys the contract no longer declares are only
    reported, not deleted.
    """
    from robotsix_central_deploy.onboard.fetcher import (  # noqa: PLC0415
        FetchError,
        fetch_repo_files,
    )
    from robotsix_central_deploy.onboard.parser import (  # noqa: PLC0415
        ParseError,
        parse_compose,
    )

    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )
    if not comp_cfg.git_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Component '{name}' has no git_url — cannot fetch its repo",
        )

    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, comp_cfg.git_url
        )
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        spec = parse_compose(repo_files.compose_bytes, name, comp_cfg.git_url)
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": exc.violations},
        ) from exc

    declared: dict[str, str] = spec.env
    stored = await env_store.get(name)
    existing_keys = set(stored.env) | set(stored.secret_tokens)
    add_env = {k: v for k, v in declared.items() if v and k not in existing_keys}
    add_secrets = {
        k: "" for k, v in declared.items() if not v and k not in existing_keys
    }
    if add_env or add_secrets:
        await env_store.upsert(name, add_env, add_secrets)
    return EnvSyncResponse(
        added_env=sorted(add_env),
        added_secrets=sorted(add_secrets),
        undeclared=sorted(existing_keys - set(declared)),
    )


# ---------------------------------------------------------------------------
# DELETE /services/{name}/env/{key}
# ---------------------------------------------------------------------------


@router.delete(
    "/services/{name}/env/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment variable or secret key for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service or key not found"},
    },
)
async def delete_service_env_key(
    name: str,
    key: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    """Delete a single environment-variable or secret key for a service.

    Returns 204 No Content on success. Raises 404 if the service or key
    is not found.
    """
    await _get_or_create_record(name, store)
    found = await env_store.delete_key(name, key)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key '{key}' not found in env or secrets for '{name}'",
        )
