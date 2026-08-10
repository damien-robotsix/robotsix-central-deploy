"""Chat agent mutation-permission endpoints.

Extracted from chat_services.py — enable/disable-mutation handlers
and the TTL auto-disable scheduler.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from .._config_utils import _sanitize_log
from .._deploy_credential import reconcile_deploy_credential
from .._langfuse_config import reconcile_langfuse_after_toggle
from ..auth import verify_auth
from ..deps import (
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config_yaml_store,
)
from ..schemas import (
    ChatAgentMutationDisableResponse,
    ChatAgentMutationEnableRequest,
    ChatAgentMutationEnableResponse,
)
from ._chat_common import logger

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Mutation TTL housekeeping
# ---------------------------------------------------------------------------

_mutation_ttl_tasks: dict[str, asyncio.Task[None]] = {}
"""Per-service asyncio Tasks that auto-disable mutation after a TTL expires."""


def _cancel_ttl_task(name: str) -> None:
    """Cancel and remove any pending TTL auto-disable task for *name*."""
    task = _mutation_ttl_tasks.pop(name, None)
    if task is not None:
        task.cancel()


def _schedule_ttl_task(
    name: str,
    ttl_seconds: int,
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
) -> None:
    """Schedule a best-effort auto-disable for *name* after *ttl_seconds*."""

    async def _auto_disable() -> None:
        await asyncio.sleep(ttl_seconds)
        try:
            cfg = component_config_store.get(name)
            if cfg is not None and cfg.chat_agent_mutatable:
                cfg.chat_agent_mutatable = False
                await component_config_store.put(cfg)
                await audit_store.append(
                    ChatAgentAuditEntry(
                        component=name,
                        action="disable-mutation",
                        detail=f"Auto-disabled after TTL ({ttl_seconds}s)",
                    )
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "TTL auto-disable for '%s' failed",
                _sanitize_log(name),
                exc_info=True,
            )
        finally:
            _mutation_ttl_tasks.pop(name, None)

    _mutation_ttl_tasks[name] = asyncio.create_task(_auto_disable())


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/enable-mutation
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/enable-mutation",
    response_model=ChatAgentMutationEnableResponse,
    summary="Grant chat-agent mutation permission for a named stub",
    responses={
        404: {"description": "Component not found"},
    },
)
async def chat_enable_mutation(
    name: str,
    body: ChatAgentMutationEnableRequest,
    request: Request,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentMutationEnableResponse:
    """Enable the per-service ``chat_agent_mutatable`` flag for *name*.

    This endpoint is **not** gated by ``_require_allowed_service`` —
    the whole point is to grant access when it is currently denied.
    Every enable writes a ``ChatAgentAuditEntry``.

    An optional ``ttl_seconds`` schedules a best-effort auto-disable
    after the given number of seconds.  The timer is cancelled if the
    server restarts.
    """
    cfg = component_config_store.get(name)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config found for '{name}'.",
        )

    previous = cfg.chat_agent_mutatable

    if previous:
        # Already enabled — still accept the TTL (extend/reset timer).
        if body.ttl_seconds is not None and body.ttl_seconds > 0:
            _cancel_ttl_task(name)
            _schedule_ttl_task(
                name, body.ttl_seconds, component_config_store, audit_store
            )
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="enable-mutation",
                detail=(
                    "chat_agent_mutatable already True (idempotent)"
                    + (
                        f"; TTL reset to {body.ttl_seconds}s"
                        if body.ttl_seconds
                        else ""
                    )
                ),
            )
        )
        return ChatAgentMutationEnableResponse(
            name=name,
            previous=True,
            current=True,
            ttl_seconds=body.ttl_seconds,
            detail="chat_agent_mutatable was already enabled (idempotent).",
        )

    cfg.chat_agent_mutatable = True
    await component_config_store.put(cfg)

    if body.ttl_seconds is not None and body.ttl_seconds > 0:
        _cancel_ttl_task(name)
        _schedule_ttl_task(name, body.ttl_seconds, component_config_store, audit_store)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="enable-mutation",
            detail=(
                "chat_agent_mutatable: False → True"
                + (f"; TTL={body.ttl_seconds}s" if body.ttl_seconds else "")
            ),
        )
    )

    # Reconcile Langfuse auto-projects — enabling mutation may add
    # project aliases discoverable from this service's config.
    await reconcile_langfuse_after_toggle(component_config_store, request)
    await reconcile_deploy_credential(component_config_store, request)

    return ChatAgentMutationEnableResponse(
        name=name,
        previous=False,
        current=True,
        ttl_seconds=body.ttl_seconds,
        detail="Chat-agent mutation enabled.",
    )


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/disable-mutation
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/disable-mutation",
    response_model=ChatAgentMutationDisableResponse,
    summary="Revoke chat-agent mutation permission for a named stub",
    responses={
        404: {"description": "Component not found"},
    },
)
async def chat_disable_mutation(
    name: str,
    request: Request,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentMutationDisableResponse:
    """Disable the per-service ``chat_agent_mutatable`` flag for *name*.

    Cancels any pending TTL auto-disable task.  Every disable writes a
    ``ChatAgentAuditEntry``.  Idempotent — disabling an already-disabled
    stub is a no-op.
    """
    cfg = component_config_store.get(name)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config found for '{name}'.",
        )

    previous = cfg.chat_agent_mutatable

    # Cancel any pending TTL task regardless.
    _cancel_ttl_task(name)

    if not previous:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="disable-mutation",
                detail="chat_agent_mutatable already False (idempotent)",
            )
        )
        return ChatAgentMutationDisableResponse(
            name=name,
            previous=False,
            current=False,
            detail="chat_agent_mutatable was already disabled (idempotent).",
        )

    cfg.chat_agent_mutatable = False
    await component_config_store.put(cfg)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="disable-mutation",
            detail="chat_agent_mutatable: True → False",
        )
    )

    # Reconcile Langfuse auto-projects — disabling mutation may remove
    # project aliases that were only discoverable from this service.
    await reconcile_langfuse_after_toggle(component_config_store, request)
    await reconcile_deploy_credential(component_config_store, request)

    return ChatAgentMutationDisableResponse(
        name=name,
        previous=True,
        current=False,
        detail="Chat-agent mutation disabled.",
    )
