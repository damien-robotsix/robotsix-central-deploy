"""Chat agent service registration endpoint.

Extracted from chat_services.py — the register-component handler.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ..auth import verify_auth
from ..deps import (
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_registry,
    _get_store,
)
from ..models import ServiceRecord, ServiceState
from ..schemas import ChatAgentRegisterRequest, ChatAgentRegisterResponse
from ..store import ServiceStore

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# POST /chat/services — register a new managed component
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services",
    response_model=ChatAgentRegisterResponse,
    summary="Register a new managed component (idempotent)",
    responses={
        403: {"description": "Registration not enabled"},
        409: {"description": "Name conflict with an existing component"},
        422: {"description": "Invalid request body"},
    },
)
async def chat_register_component(
    body: ChatAgentRegisterRequest,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentRegisterResponse:
    """Register a new managed component in the service inventory.

    Creates a minimal ``ComponentConfig`` from the request body and
    persists it, alongside a ``ServiceRecord`` in STOPPED state.
    The component is NOT auto-started — this endpoint only registers
    metadata.

    Idempotent: re-registering an existing component id returns the
    existing entry unchanged.

    Requires ``chat_agent_registration_enabled`` to be ``True`` in the
    server config.
    """
    lifecycle_config = await _get_config(request)
    if not lifecycle_config.chat_agent_registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chat agent registration is not enabled on this server.",
        )

    # Reject reserved names that would shadow API routes.
    from ...registry.constants import RESERVED_NAMES

    if body.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Component name '{body.name}' is reserved.",
        )

    existing = component_config_store.get(body.name)
    if existing is not None:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=body.name,
                action="register",
                detail=f"Re-registration (idempotent) — image={body.image}, owner_repo={body.owner_repo}",
            )
        )
        return ChatAgentRegisterResponse(
            name=existing.id,
            image=existing.image,
            owner_repo=existing.git_url,
            detail="Component already registered (idempotent).",
            existed=True,
        )

    # Validate the deploy contract — fetch the repo, parse compose,
    # and enforce the central-deploy-contract-version header.
    # This mirrors the manual /onboard/preflight path so the agent
    # register endpoint cannot bypass the supply-chain gate.
    loop = asyncio.get_running_loop()
    from ..deps._compose_resolver import _resolve_compose_backbone

    await _resolve_compose_backbone(body.owner_repo, body.name, lifecycle_config, loop)

    # Derive a container_name from the component id.
    container_name = body.name
    # If the image includes a tag, use it; otherwise default to ':latest'.
    image_ref = body.image if ":" in body.image else f"{body.image}:latest"

    comp_cfg = ComponentConfig(
        id=body.name,
        image=image_ref,
        container_name=container_name,
        git_url=body.owner_repo,
    )
    await component_config_store.put(comp_cfg)
    registry.register(comp_cfg)

    # Create a ServiceRecord in STOPPED state so the component appears
    # in GET /services inventory.
    record = ServiceRecord(
        name=body.name,
        image=image_ref,
        container_name=container_name,
        state=ServiceState.STOPPED,
    )
    await store.put(record)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=body.name,
            action="register",
            detail=f"Registered — image={image_ref}, owner_repo={body.owner_repo}",
        )
    )

    return ChatAgentRegisterResponse(
        name=body.name,
        image=image_ref,
        owner_repo=body.owner_repo,
        detail="Component registered. Start and deploy are separate gated actions.",
    )
