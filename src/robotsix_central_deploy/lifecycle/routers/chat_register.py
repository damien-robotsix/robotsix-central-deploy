"""Chat agent service registration endpoint.

Extracted from chat_services.py — the register-component handler.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...onboard.models import DerivedSpec
from ...onboard.port_utils import collect_occupied_host_ports, find_free_host_port
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.loader import ComponentRegistry
from ..auth import verify_auth
from ..deps import (
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_registry,
    _get_store,
)
from ..models import ServiceRecord, ServiceState
from ..schemas import ChatAgentRegisterRequest, ChatAgentRegisterResponse
from ..store import ServiceStore

router = APIRouter(tags=["chat"])


def _assign_free_host_ports(
    spec: DerivedSpec,
    component_config_store: ComponentConfigStore,
    lifecycle_port: int,
) -> None:
    """Shift *spec*'s host ports off any already claimed by another component.

    A manifest states the host port its author picked, which says nothing
    about what this machine has free. Onboarding resolves that at
    /onboard/preflight; registration reaches the same store and must resolve
    it the same way, or two components bind the same port and the second one
    fails to start.
    """
    occupied = collect_occupied_host_ports(component_config_store, lifecycle_port)
    for pm in [*spec.ports, *(pm for sib in spec.siblings for pm in sib.ports)]:
        if pm.host in occupied:
            pm.host = find_free_host_port(occupied)
        occupied.add(pm.host)


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
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentRegisterResponse:
    """Register a new managed component in the service inventory.

    Builds the ``ComponentConfig`` from the repo's parsed
    ``deploy/docker-compose.yml`` — ports, mounts, named volumes, config
    volume, health check and siblings all included — and persists it
    alongside a ``ServiceRecord`` in STOPPED state. The component is NOT
    auto-started; this endpoint only registers it.

    Host ports are shifted off any this machine has already claimed, exactly
    as ``/onboard/preflight`` does.

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

    # Resolve the deploy contract — fetch the repo, parse compose, and
    # enforce the central-deploy-contract-version header. This mirrors the
    # manual /onboard/preflight path so the agent register endpoint cannot
    # bypass the supply-chain gate.
    loop = asyncio.get_running_loop()
    from ..deps._compose_resolver import _resolve_compose_backbone
    from ..deps.seed import (
        _build_component_config_from_spec,
        _namespace_spec_volumes,
    )

    _repo_files, spec = await _resolve_compose_backbone(
        body.owner_repo, body.name, lifecycle_config, loop
    )

    # Derive a container_name from the component id.
    container_name = body.name
    # If the image includes a tag, use it; otherwise default to ':latest'.
    image_ref = body.image if ":" in body.image else f"{body.image}:latest"

    # The parsed spec is what makes the component *deployable*, so it is built
    # into the stored config rather than discarded. Registering the bare
    # id/image/repo triple instead produced a component that passed the
    # contract gate and then deployed with no ports, no mounts and no config
    # volume: Traefik emits no route for a component with no port (the public
    # URL 404s while the container reports healthy), and its data lands in the
    # container's writable layer, to be destroyed by the next redeploy. Both
    # failures are silent, so they surface only once someone trusts the
    # service with data.
    spec = _namespace_spec_volumes(spec, body.name)
    _assign_free_host_ports(spec, component_config_store, lifecycle_config.port)

    comp_cfg = _build_component_config_from_spec(
        spec,
        git_url=body.owner_repo,
        # The agent names the image it wants deployed; the manifest only
        # states the one its author committed. Registration has always taken
        # the caller's, and the response still reports it back.
        image=image_ref,
        container_name=container_name,
    )
    await component_config_store.put(comp_cfg)
    registry.register(comp_cfg)

    # Store the schema the repo ships, as onboarding does. Without it the
    # component has no template, so the first deploy finds nothing to seed
    # into its empty config volume and it starts on whatever defaults are
    # compiled into the image — which are not necessarily the paths its
    # deploy contract mounts volumes at.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(body.name, spec.config_schema)

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
