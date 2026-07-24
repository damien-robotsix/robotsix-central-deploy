"""Environment variable and secret management endpoints for the lifecycle server."""

from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_auth
from ..deps import (
    _fetch_component_repo_files,
    _get_component_config_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_store,
)
from ..models import ErrorDetail
from ..schemas import EnvResponse, EnvSyncResponse, EnvUpdate
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry

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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> EnvResponse:
    """Return stored environment variables and masked secret keys for a service.

    Secret values are never exposed — only the key names are returned, each
    masked as ``"***"``. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    config = await env_store.get(name)
    secrets_masked = {key: "***" for key in config.secret_tokens}
    comp_cfg = component_config_store.get(name)
    mem_limit = comp_cfg.mem_limit if comp_cfg else "2g"
    allow_chat_access = comp_cfg.allow_chat_access if comp_cfg else False
    claude_mount = comp_cfg.claude_mount if comp_cfg else False
    return EnvResponse(
        env=config.env,
        secrets=secrets_masked,
        env_scopes=config.env_scopes,
        secret_scopes=config.secret_scopes,
        mem_limit=mem_limit,
        allow_chat_access=allow_chat_access,
        claude_mount=claude_mount,
    )


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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> None:
    """Create or update environment variables and secrets for a service.

    Optionally update the memory limit for the component's container.
    Returns 204 No Content on success. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    await env_store.upsert(
        name,
        body.env,
        body.secrets,
        env_scopes=body.env_scopes,
        secret_scopes=body.secret_scopes,
    )
    if body.mem_limit is not None:
        comp_cfg = component_config_store.get(name)
        if comp_cfg is not None:
            comp_cfg.mem_limit = body.mem_limit
            await component_config_store.put(comp_cfg)
            registry.register(comp_cfg)
    if body.allow_chat_access is not None:
        comp_cfg = component_config_store.get(name)
        if comp_cfg is not None:
            comp_cfg.allow_chat_access = body.allow_chat_access
            await component_config_store.put(comp_cfg)
            registry.register(comp_cfg)
    if body.claude_mount is not None:
        comp_cfg = component_config_store.get(name)
        if comp_cfg is not None:
            comp_cfg.claude_mount = body.claude_mount
            await component_config_store.put(comp_cfg)
            registry.register(comp_cfg)


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
    are never modified. Keys the contract no longer declares are reported
    as ``undeclared`` but left in the store (never deleted).
    """
    from robotsix_central_deploy.onboard.parser import (  # noqa: PLC0415
        ParseError,
        parse_compose,
    )

    comp_cfg, repo_files = await _fetch_component_repo_files(
        name, component_config_store
    )
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
    undeclared_keys = sorted(existing_keys - set(declared))
    if add_env or add_secrets:
        await env_store.upsert(name, add_env, add_secrets)
    return EnvSyncResponse(
        added_env=sorted(add_env),
        added_secrets=sorted(add_secrets),
        undeclared=undeclared_keys,
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
