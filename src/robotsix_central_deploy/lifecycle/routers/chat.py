"""Chat agent component roster and scoped write-surface endpoints.

Exposes:
- ``GET /chat/components`` — list components reachable by the chat agent
- ``PUT /chat/config/{name}`` — update non-secret config keys (allowlisted services only)
- ``POST /chat/config/{name}/rollback`` — restore previous config version
- ``POST /chat/services/{name}/restart`` — restart a service (allowlisted)
- ``POST /chat/services/{name}/update`` — pull + recreate (deploy) a service (allowlisted)
- ``GET /chat/audit-log`` — read recent audit entries
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
    _validate_config_or_422,
)
from ._sibling_utils import _fanout_siblings_best_effort
from .._config_utils import _mask_secrets, _merge_config, _canonical_hash
from ..models import (
    ActionType,
    ServiceState,
    can_transition,
)
from ..schemas import (
    ChatAgentAuditEntryResponse,
    ChatAgentAuditLogResponse,
    ChatAgentConfigRollbackResponse,
    ChatAgentConfigUpdate,
    ChatAgentRestartResponse,
    ChatAgentUpdateResponse,
)
from ..store import ServiceStore
from ...deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ...registry.settings_store import VALID_LOG_LEVELS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Simple TTL cache for skill bodies: {component_id: (timestamp, body)}
_skill_cache: dict[str, tuple[float, str]] = {}
_SKILL_CACHE_TTL: float = 60.0


# ---------------------------------------------------------------------------
# Auth metadata injection helper
# ---------------------------------------------------------------------------


def _inject_auth(entry: dict[str, Any], comp_cfg: ComponentConfig) -> None:
    """Attach an ``auth`` sub-dict to *entry* when the component carries
    auth metadata the chat agent can use to authenticate requests.

    The auth dict references **environment variable names** — the chat
    agent resolves them at runtime from its own container environment
    (which the deploy server populates via the EnvStore at deploy time).
    Actual credential values are never embedded in the roster response.
    """
    if not comp_cfg.auth_type:
        return
    auth: dict[str, str] = {"type": comp_cfg.auth_type}
    if comp_cfg.auth_type == "basic":
        if comp_cfg.auth_username_env:
            auth["username_env"] = comp_cfg.auth_username_env
        if comp_cfg.auth_password_env:
            auth["password_env"] = comp_cfg.auth_password_env
    elif comp_cfg.auth_type == "header":
        if comp_cfg.auth_header_name:
            auth["header_name"] = comp_cfg.auth_header_name
        if comp_cfg.auth_token_env:
            auth["token_env"] = comp_cfg.auth_token_env
    entry["auth"] = auth


# ---------------------------------------------------------------------------
# GET /chat-skill — the deploy server's own skill, so the chat agent can
# discover the deploy component itself.
# ---------------------------------------------------------------------------


@router.get(
    "/chat-skill",
    summary="Deploy server's own chat-agent skill description",
    responses={200: {"description": "Markdown skill body"}},
)
async def deploy_chat_skill() -> str:
    """Return the Markdown skill description for the deploy server itself.

    This lets the deploy server register as a virtual component with
    ``chat_base_url`` pointing at itself, and the roster endpoint's
    probe succeeds against this handler.
    """
    return (
        "# Deploy Lifecycle Server\n"
        "Manages the robotsix Docker fleet: start, stop, restart, deploy, "
        "rollback, and inspect every managed component.\n\n"
        "## Authentication\n"
        "All requests require an `X-API-Key` header.  The chat agent reads "
        "this key from the `DEPLOY_API_KEY` environment variable (injected "
        "by the deploy server itself at container start).\n\n"
        "## Read-only endpoints\n"
        "- `GET /services` — list all managed services\n"
        "- `GET /services/{name}` — full status (state, image, health, digests)\n"
        "- `GET /services/{name}/health` — health status string\n"
        "- `GET /services/{name}/logs` — stream container logs\n"
        "- `GET /health` — liveness probe\n"
        "- `GET /disk` — host disk usage + Docker storage breakdown\n"
        "- `GET /chat/components` — list components reachable by the chat agent\n"
        "- `GET /chat/audit-log` — read recent audit entries\n"
        "- `GET /chat/langfuse/projects` — list configured Langfuse project aliases\n"
        "- `GET /chat/langfuse/{project}/traces` — list Langfuse traces for a project\n"
        "- `GET /chat/langfuse/{project}/traces/{traceId}` — single Langfuse trace detail\n"
        "- `GET /chat/langfuse/{project}/observations` — list Langfuse observations\n"
        "- `GET /chat/langfuse/{project}/observations/{observationId}` — single observation detail\n\n"
        "## Scoped write endpoints (chat-agent allowlisted)\n"
        "- `PUT /chat/config/{name}` — update non-secret config keys\n"
        "- `POST /chat/config/{name}/rollback` — restore previous config version\n"
        "- `POST /chat/services/{name}/restart` — restart a service\n"
        "- `POST /chat/services/{name}/update` — pull + recreate (deploy) a service\n\n"
        "## Agent self-restart\n"
        "The robotsix-chat agent can restart itself via:\n"
        "`POST /chat/services/chat/restart`\n"
        "This is needed after the component roster is updated so the agent "
        "picks up newly registered virtual components."
    )


# ---------------------------------------------------------------------------
# Server-side allowlists
# ---------------------------------------------------------------------------

# Services the chat agent is permitted to mutate. The chat service's real
# registered name is "chat" (see GET /services), not "robotsix-chat" — using
# the wrong name here silently 404'd the agent's own documented self-restart
# path (POST /chat/services/robotsix-chat/restart) while the correct name
# ("chat") was rejected as not-allowlisted (403), so neither ever worked.
_CHAT_ALLOWED_SERVICES: frozenset[str] = frozenset({"chat", "cognee"})

# Rate-limit cooldowns (seconds) per action type.
_RATE_LIMIT_COOLDOWNS: dict[str, float] = {
    "restart": 60.0,
    "update": 300.0,
    "config_update": 5.0,
    "config_rollback": 10.0,
}


# ---------------------------------------------------------------------------
# Rate limiter helper
# ---------------------------------------------------------------------------


def _check_rate_limit(app_state: Any, service: str, action: str) -> None:
    """Raise HTTP 429 if *action* on *service* is within the cooldown window."""
    cooldown = _RATE_LIMIT_COOLDOWNS.get(action, 30.0)
    key = f"{service}:{action}"
    rate_limits: dict[str, float] = getattr(app_state, "chat_agent_rate_limits", {})
    now = time.monotonic()
    if key in rate_limits and now - rate_limits[key] < cooldown:
        remaining = cooldown - (now - rate_limits[key])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit: {action} on '{service}' is allowed once every "
                f"{cooldown:.0f}s. Retry in {remaining:.1f}s."
            ),
        )
    rate_limits[key] = now


# ---------------------------------------------------------------------------
# Service allowlist guard
# ---------------------------------------------------------------------------


def _require_allowed_service(name: str) -> None:
    """Raise HTTP 403 when *name* is not in the chat-agent service allowlist."""
    if name not in _CHAT_ALLOWED_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Chat agent is not permitted to mutate service '{name}'.",
        )


# ---------------------------------------------------------------------------
# Secret-free key guard
# ---------------------------------------------------------------------------


def _reject_secret_keys(schema: dict[str, Any], submitted: dict[str, Any]) -> None:
    """Raise HTTP 403 when *submitted* contains any secret-typed keys.

    Secrets are detected via ``"format": "password"`` + ``"writeOnly": true``
    in the JSON Schema properties.
    """
    from .._config_utils import _is_json_schema, _is_secret_prop, _resolve_ref

    if not _is_json_schema(schema):
        return  # legacy flat template — no secret annotations

    def _check(prop_schema: dict[str, Any], vals: dict[str, Any]) -> None:
        for key, prop in prop_schema.get("properties", {}).items():
            resolved = _resolve_ref(prop, schema)  # resolve against root schema
            if _is_secret_prop(resolved) and key in vals:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Chat agent cannot mutate secret key '{key}'.",
                )
            if resolved.get("type") == "object" and isinstance(vals.get(key), dict):
                _check(resolved, vals[key])

    _check(schema, submitted)


# ---------------------------------------------------------------------------
# GET /chat/components
# ---------------------------------------------------------------------------


@router.get(
    "/chat/components",
    summary="List components reachable by the chat agent",
    responses={401: {"description": "Unauthorized"}},
)
async def list_chat_components(
    request: Request,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """Return a roster of components the chat agent can interact with.

    Each entry has ``id``, ``base_url``, and ``skill`` (the Markdown body
    fetched live from the component's ``GET /chat-skill``).  Components
    that do not have ``allow_chat_access`` enabled are omitted.  A
    component whose skill probe fails is served from its last-known-good
    cached skill when one exists (stale-while-error), so a transient
    probe failure does not drop it from the roster; it is omitted only
    when it has never been probed successfully.

    Skill bodies are cached for 60 seconds; a component whose cached
    entry has expired is re-probed on the next request.
    """
    results: list[dict[str, Any]] = []
    now = time.monotonic()

    for comp_cfg in component_config_store.all():
        if not comp_cfg.allow_chat_access:
            continue
        # Virtual components (with chat_base_url set) don't need ports;
        # Docker components do.
        if not comp_cfg.chat_base_url and not comp_cfg.ports:
            continue

        base_url = comp_cfg.chat_base_url or (
            f"http://{comp_cfg.container_name}:{comp_cfg.ports[0].container}"
        )
        skill_endpoint = comp_cfg.chat_skill_endpoint

        # Static skill body — no probing needed.
        if comp_cfg.chat_skill:
            entry: dict[str, Any] = {
                "id": comp_cfg.id,
                "base_url": base_url,
                "skill": comp_cfg.chat_skill,
            }
            _inject_auth(entry, comp_cfg)
            results.append(entry)
            continue

        # Check cache first
        cached = _skill_cache.get(comp_cfg.id)
        if cached is not None:
            cached_at, cached_body = cached
            if now - cached_at < _SKILL_CACHE_TTL:
                entry = {
                    "id": comp_cfg.id,
                    "base_url": base_url,
                    "skill": cached_body,
                }
                _inject_auth(entry, comp_cfg)
                results.append(entry)
                continue

        # Probe the component's chat-skill endpoint
        skill_body: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base_url}{skill_endpoint}")
            if resp.status_code == 200 and resp.text.strip():
                skill_body = resp.text
                _skill_cache[comp_cfg.id] = (now, skill_body)
            else:
                logger.warning(
                    "chat components: skill probe for %s (%s) returned %s",
                    comp_cfg.id,
                    base_url,
                    resp.status_code,
                )
        except Exception as exc:
            logger.warning(
                "chat components: skill probe failed for %s (%s): %s",
                comp_cfg.id,
                base_url,
                exc,
            )

        if skill_body is None and cached is not None:
            # Stale-while-error: serve the expired cached skill rather than
            # dropping the component from the roster; the stale timestamp is
            # kept so the next request re-probes.
            skill_body = cached[1]

        if skill_body is not None:
            entry = {
                "id": comp_cfg.id,
                "base_url": base_url,
                "skill": skill_body,
            }
            _inject_auth(entry, comp_cfg)
            results.append(entry)

    return results


# ---------------------------------------------------------------------------
# PUT /chat/config/{name}
# ---------------------------------------------------------------------------


@router.put(
    "/chat/config/{name}",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Update non-secret config keys for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted or secret key rejected"},
        404: {"description": "Service has no config schema"},
        429: {"description": "Rate limited"},
    },
)
async def chat_update_config(
    name: str,
    body: ChatAgentConfigUpdate,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Update non-secret config keys for an allowlisted service.

    Saves the current config as a rollback snapshot before applying the
    update.  Secret keys are rejected with 403.  Writes the merged config
    to the service's config volume and records an audit entry for every
    changed key.
    """
    _require_allowed_service(name)
    _check_rate_limit(request.app.state, name, "config_update")

    # ------------------------------------------------------------------
    # log_level is a system-wide setting that the chat agent can raise
    # or lower during troubleshooting.  Apply it immediately to the root
    # logger and strip it from the submitted values so it does not
    # collide with component-level config schema validation.
    # ------------------------------------------------------------------
    if "log_level" in body.values:
        raw_level = str(body.values.pop("log_level")).upper()
        if raw_level not in VALID_LOG_LEVELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": (
                        f"Unknown log level '{raw_level}'. "
                        f"Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
                    )
                },
            )
        logging.getLogger().setLevel(raw_level)
        logger.info(
            "Chat agent set log_level to %s via /chat/config/%s", raw_level, name
        )  # codeql[py/log-injection]: raw_level validated against VALID_LOG_LEVELS above
        if not body.values:
            # Only log_level was submitted — nothing to write to the
            # component config volume.
            return ChatAgentConfigRollbackResponse(
                component=name,
                restored={},
                detail=f"log_level set to {raw_level}; no component config keys changed.",
            )

    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )

    # Reject any secret keys in the submitted values.
    _reject_secret_keys(template, body.values)

    # Snapshot current config for rollback before mutating.
    existing = await config_yaml_store.get_current(name)
    if existing is not None:
        await config_yaml_store.save_previous(name, existing)
    else:
        # No current config yet — snapshot the template defaults so the
        # operator can still roll back the first change.
        default_config = _merge_config(template, {}, {})
        await config_yaml_store.save_previous(name, default_config)

    # Merge submitted values over existing (or template defaults when no
    # current config exists yet).
    try:
        merged = _merge_config(template, existing or {}, body.values)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc)},
        )

    _validate_config_or_422(template, merged)

    # Write to volume if available.
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, merged)
        new_hash = _canonical_hash(merged)
        await config_yaml_store.update_current_and_hash(name, merged, new_hash)
    else:
        await config_yaml_store.update_current(name, merged)

    # Audit-log each changed key.
    for key, new_val in body.values.items():
        old_val = existing.get(key) if isinstance(existing, dict) else None
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="config_update",
                key=key,
                old_value=old_val,
                new_value=new_val,
            )
        )

    masked = _mask_secrets(template, merged)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Config updated; previous version saved for rollback.",
    )


# ---------------------------------------------------------------------------
# POST /chat/config/{name}/rollback
# ---------------------------------------------------------------------------


@router.post(
    "/chat/config/{name}/rollback",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Restore the previous config version for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "No previous config snapshot available"},
        429: {"description": "Rate limited"},
    },
)
async def chat_rollback_config(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Restore the previous config snapshot for an allowlisted service.

    Returns 404 when no previous snapshot exists (e.g. no config update
    has been performed yet through the chat surface).
    """
    _require_allowed_service(name)
    _check_rate_limit(request.app.state, name, "config_rollback")

    previous = await config_yaml_store.get_previous(name)
    if previous is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No previous config snapshot for component '{name}'.",
        )

    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )

    _validate_config_or_422(template, previous)

    # Write the previous config back.
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, previous)
        new_hash = _canonical_hash(previous)
        await config_yaml_store.update_current_and_hash(name, previous, new_hash)
    else:
        await config_yaml_store.update_current(name, previous)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="config_rollback",
            detail="Restored previous config snapshot.",
        )
    )

    masked = _mask_secrets(template, previous)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Config rolled back to previous snapshot.",
    )


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/restart",
    response_model=ChatAgentRestartResponse,
    summary="Restart an allowlisted service (idempotent)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Invalid state transition"},
        429: {"description": "Rate limited"},
    },
)
async def chat_restart_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentRestartResponse:
    """Restart an allowlisted service. Idempotent.

    Raises 403 if the service is not in the chat-agent allowlist.
    Rate-limited to one restart per 60 seconds per service.
    """
    _require_allowed_service(name)
    _check_rate_limit(request.app.state, name, "restart")

    record = await _get_or_create_record(name, store)
    previous = record.state

    if record.state == ServiceState.RESTARTING:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail="Restart already in progress.",
            )
        )
        return ChatAgentRestartResponse(
            name=name,
            previous_state=previous.value,
            current_state=ServiceState.RESTARTING.value,
            detail="Restart already in progress",
        )

    if not can_transition(record.state, ServiceState.RESTARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot restart from state '{record.state.value}'",
        )

    record.state = ServiceState.RESTARTING
    await store.put(record)

    try:
        final_state = await backend.restart(record)
    except Exception as exc:
        logger.exception("chat restart %s failed", name.replace("\n", "\\n"))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail=f"Restart failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
    await store.put(record)

    # Restart siblings (best-effort).
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "restart")

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action=ActionType.RESTART,
            detail=f"Restarted: {previous.value} → {final_state.value}",
        )
    )

    return ChatAgentRestartResponse(
        name=name,
        previous_state=previous.value,
        current_state=record.state.value,
    )


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/update
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/update",
    response_model=ChatAgentUpdateResponse,
    summary="Pull + recreate (deploy) an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_update_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentUpdateResponse:
    """Pull the latest image and recreate the container for an allowlisted service.

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one update per 300 seconds per service.
    """
    _require_allowed_service(name)
    _check_rate_limit(request.app.state, name, "update")

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}'.",
        )

    # Merge env overrides from the env store (same as the main deploy endpoint).
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    # Serialise concurrent deploys.
    if not await try_acquire_deploy_lock(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{name}'.",
        )

    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("chat update %s failed", name.replace("\n", "\\n"))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="update",
                detail=f"Update failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update failed: {exc}",
        )
    finally:
        release_deploy_lock(name)

    record.state = outcome.state
    record.image = config.image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Deploy siblings (best-effort) so the whole component group converges.
    updated_siblings: list[str] = []
    if config.siblings:
        for sib_cfg, sib_record in await _get_sibling_pairs(name, config, store):
            sib_name = f"{name}-{sib_cfg.service_key}"
            try:
                sib_merged_env = await env_store.get_merged_env(sib_name, sib_cfg.env)
                sib_effective = config.model_copy(
                    update={
                        "id": sib_name,
                        "image": sib_cfg.image,
                        "container_name": sib_cfg.container_name,
                        "ports": sib_cfg.ports,
                        "mounts": sib_cfg.mounts,
                        "env": sib_merged_env,
                        "health_check": sib_cfg.health_check,
                        "claude_mount": sib_cfg.claude_mount,
                        "claude_mount_path": sib_cfg.claude_mount_path,
                        "host_docker_sock": sib_cfg.host_docker_sock,
                        "named_volumes": [m.host for m in sib_cfg.mounts],
                        "command": sib_cfg.command,
                        "entrypoint": sib_cfg.entrypoint,
                        "tmpfs": sib_cfg.tmpfs,
                        "mem_limit": sib_cfg.mem_limit,
                        "user": sib_cfg.user,
                    }
                )
                sib_outcome = await backend.deploy(
                    sib_record, sib_effective, sib_cfg.image
                )
                sib_record.state = sib_outcome.state
                sib_record.image = sib_cfg.image
                sib_record.deployed_image_digest = sib_outcome.deployed_digest
                sib_record.previous_image_digest = sib_outcome.previous_digest
                await store.put(sib_record)
                updated_siblings.append(sib_name)
            except Exception:
                logger.warning(
                    "chat update: deploy sibling '%s' failed",
                    sib_name.replace("\n", "\\n"),
                )

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="update",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
                + (
                    f"; siblings: {', '.join(updated_siblings)}"
                    if updated_siblings
                    else ""
                )
            ),
        )
    )

    return ChatAgentUpdateResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        detail="Update completed."
        + (f" Also updated: {', '.join(updated_siblings)}" if updated_siblings else ""),
        updated_siblings=updated_siblings,
    )


# ---------------------------------------------------------------------------
# GET /chat/audit-log
# ---------------------------------------------------------------------------


@router.get(
    "/chat/audit-log",
    response_model=ChatAgentAuditLogResponse,
    summary="Read recent chat-agent mutation audit entries",
    responses={401: {"description": "Unauthorized"}},
)
async def chat_audit_log(
    request: Request,
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    limit: int = 50,
    component: str | None = None,
    _auth: None = Depends(verify_auth),
) -> ChatAgentAuditLogResponse:
    """Return recent chat-agent audit entries, most-recent-first.

    Optionally filter by *component* name.
    """
    entries = await audit_store.list(limit=limit, component=component)
    return ChatAgentAuditLogResponse(
        entries=[
            ChatAgentAuditEntryResponse(
                timestamp=e.timestamp,
                agent_id=e.agent_id,
                component=e.component,
                action=e.action,
                key=e.key,
                old_value=e.old_value,
                new_value=e.new_value,
                detail=e.detail,
            )
            for e in entries
        ]
    )
