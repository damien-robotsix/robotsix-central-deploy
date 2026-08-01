"""Fleet-internal Langfuse credentials endpoint.

Exposes per-component Langfuse project credentials read from each
service's standardized config so fleet consumers (cost-monitor, …)
can access trace data without duplicating key pairs.

Exposes:
- ``GET /fleet/langfuse`` — enumerate components and their Langfuse credentials
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from ..auth import verify_auth
from ..deps import _get_config_yaml_store, _get_registry, _get_store
from ..models import ServiceState
from ..store import ServiceStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.loader import ComponentRegistry

router = APIRouter(tags=["fleet-langfuse"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FleetLangfuseProject(BaseModel):
    """Credentials for one Langfuse project belonging to a component."""

    alias: str = Field(description="Langfuse project alias / slug")
    public_key: str = Field(description="Langfuse public key (read-only)")
    secret_key: str = Field(description="Langfuse secret key (read-only)")


class FleetLangfuseComponent(BaseModel):
    """One component's Langfuse configuration, when available."""

    component_id: str = Field(
        description="Stable component slug (e.g. 'robotsix-chat')"
    )
    name: str = Field(description="Container / service name")
    status: str = Field(description="Current lifecycle state")
    langfuse_host: str | None = Field(
        default=None, description="Langfuse instance base URL, or None"
    )
    projects: list[FleetLangfuseProject] = Field(
        default_factory=list,
        description="Langfuse projects with credentials (empty when not configured)",
    )


class FleetLangfuseResponse(BaseModel):
    """Top-level response for GET /fleet/langfuse."""

    components: list[FleetLangfuseComponent] = Field(
        description="Every registered component; Langfuse fields are populated when configured"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_langfuse_projects(
    current: dict[str, object],
) -> tuple[str | None, list[FleetLangfuseProject]]:
    """Extract Langfuse host and project credentials from a component config dict.

    Reads ``langfuse_base_url`` (host) and ``langfuse_projects``
    (dict of alias → {public_key, secret_key}) from *current*.

    Returns ``(host, projects)`` where *host* is a string or None and
    *projects* is a list of ``FleetLangfuseProject`` (only those with
    both keys set).
    """
    host: str | None = None
    raw_host = current.get("langfuse_base_url")
    if isinstance(raw_host, str) and raw_host:
        host = raw_host

    projects: list[FleetLangfuseProject] = []
    raw_projects = current.get("langfuse_projects")
    if isinstance(raw_projects, dict):
        for alias, creds in raw_projects.items():
            if not isinstance(creds, dict):
                continue
            pk = creds.get("public_key", "")
            sk = creds.get("secret_key", "")
            if pk and sk:
                projects.append(
                    FleetLangfuseProject(alias=alias, public_key=pk, secret_key=sk)
                )

    return host, projects


# ---------------------------------------------------------------------------
# GET /fleet/langfuse
# ---------------------------------------------------------------------------


@router.get(
    "/fleet/langfuse",
    response_model=FleetLangfuseResponse,
    summary="List components with Langfuse credentials",
)
async def fleet_langfuse_credentials(
    store: ServiceStore = Depends(_get_store),
    registry: ComponentRegistry = Depends(_get_registry),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    _auth: None = Depends(verify_auth),
) -> FleetLangfuseResponse:
    """Return every registered component with its lifecycle status and,
    when configured, the Langfuse host and project API key pairs
    needed to read that component's traces.

    Only authenticated fleet-internal consumers may call this endpoint.
    """
    records = await store.list_all()
    record_by_name: dict[str, ServiceState] = {r.name: r.state for r in records}

    components: list[FleetLangfuseComponent] = []

    for comp in registry.all():
        state = record_by_name.get(comp.container_name, ServiceState.UNKNOWN)

        # Try to read the component's standardized config for Langfuse keys.
        current = await config_yaml_store.get_current(comp.id)
        if current:
            host, projects = _extract_langfuse_projects(current)
        else:
            host, projects = None, []

        components.append(
            FleetLangfuseComponent(
                component_id=comp.id,
                name=comp.container_name,
                status=state.value,
                langfuse_host=host,
                projects=projects,
            )
        )

    return FleetLangfuseResponse(components=components)
