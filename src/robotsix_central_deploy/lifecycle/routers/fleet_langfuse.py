"""Fleet-internal Langfuse credentials endpoint.

Exposes per-component Langfuse project credentials read from each
service's standardized config so fleet consumers (cost-monitor, …)
can access trace data without duplicating key pairs.

Credentials come from two sources, merged exactly as the chat-agent trace
proxy merges them (see :mod:`.chat_langfuse`):

1. **Component-declared** — the canonical ``langfuse.projects`` block in each
   component's standardized config.  This is the destination shape; a
   component that has migrated needs nothing else.
2. **Operator-configured** — ``LifecycleConfig.langfuse_projects``, central
   deploy's own config.  These take precedence, so the operator can pin or
   rotate a key, and can serve credentials for components that have not yet
   migrated their config to the canonical block.

Before this, only the chat proxy honoured source 2 while this endpoint saw
source 1 alone — so the same credential concept resolved differently
depending on which consumer asked.

Exposes:
- ``GET /fleet/langfuse`` — enumerate components and their Langfuse credentials
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from .._config_utils import read_component_config
from .._langfuse_config import extract_langfuse_block
from .._openrouter_config import extract_openrouter_keys
from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_backend, _get_config, _get_registry, _get_store
from ..models import ServiceState
from ..store import ServiceStore
from ..backends.base import ExecutionBackend
from ...registry.loader import ComponentRegistry

router = APIRouter(tags=["fleet-langfuse"])

#: ``component_id`` reported for operator-configured credentials that no
#: registered component declares.  They are real projects with no owning
#: component config, so they are surfaced under a synthetic entry rather than
#: attributed to a component that never declared them.
OPERATOR_COMPONENT_ID = "operator-configured"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FleetLangfuseProject(BaseModel):
    """Credentials for one Langfuse project belonging to a component."""

    alias: str = Field(description="Langfuse project name (the `projects` key)")
    public_key: str = Field(description="Langfuse public key (read-only)")
    secret_key: str = Field(description="Langfuse secret key (read-only)")
    project_id: str | None = Field(
        default=None,
        description="Langfuse project id, when the component records it",
    )
    openrouter_key: str | None = Field(
        default=None,
        description=(
            "OpenRouter API key for this LLM function, when configured. Lets a "
            "consumer reconcile provider-billed spend against traced spend."
        ),
    )


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

    Reads the canonical ``langfuse`` block (``host`` plus a ``projects``
    map of alias → {public_key, secret_key, project_id}) via
    :func:`~.._langfuse_config.extract_langfuse_block`, the single
    definition of that shape.

    Returns ``(host, projects)`` where *host* is a string or None and
    *projects* is a list of ``FleetLangfuseProject`` (only those with
    both keys set).
    """
    host, entries = extract_langfuse_block(current)
    openrouter_keys = extract_openrouter_keys(current)
    return host, [
        FleetLangfuseProject(
            alias=entry.alias,
            public_key=entry.public_key,
            secret_key=entry.secret_key,
            project_id=entry.project_id,
            openrouter_key=openrouter_keys.get(entry.alias),
        )
        for entry in entries
    ]


def _operator_projects(config: LifecycleConfig) -> dict[str, FleetLangfuseProject]:
    """Return operator-configured projects keyed by alias.

    Half-filled entries are skipped, matching
    :func:`~.._langfuse_config.extract_langfuse_block`: a project missing
    either key is unconfigured, not a broken credential to hand out.
    """
    out: dict[str, FleetLangfuseProject] = {}
    for alias, creds in config.langfuse_projects.items():
        secret = creds.secret_key.get_secret_value()
        if not creds.public_key or not secret:
            continue
        out[alias] = FleetLangfuseProject(
            alias=alias,
            public_key=creds.public_key,
            secret_key=secret,
        )
    return out


def _operator_openrouter_keys(config: LifecycleConfig) -> dict[str, str]:
    """Return operator-configured OpenRouter keys by alias, empties dropped."""
    return {
        alias: secret
        for alias, raw in config.openrouter_keys.items()
        if (secret := raw.get_secret_value())
    }


def _with_openrouter(
    projects: list[FleetLangfuseProject], operator_keys: dict[str, str]
) -> list[FleetLangfuseProject]:
    """Overlay operator-configured OpenRouter keys onto *projects* by alias.

    Applied after the project credentials are resolved so an operator entry
    can supply a key for a component that declares Langfuse projects but no
    ``openrouter`` block — the common case until components migrate.
    """
    return [
        p.model_copy(update={"openrouter_key": operator_keys[p.alias]})
        if p.alias in operator_keys
        else p
        for p in projects
    ]


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
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> FleetLangfuseResponse:
    """Return every registered component with its lifecycle status and,
    when configured, the Langfuse host and project API key pairs
    needed to read that component's traces.

    Each project also carries its OpenRouter key when one is configured, so a
    consumer can reconcile provider-billed spend against traced spend for the
    same LLM function without a second lookup.

    Component-declared credentials are overlaid with operator-configured
    ones (``LifecycleConfig.langfuse_projects`` /
    ``LifecycleConfig.openrouter_keys``), which win on alias collision — the
    same precedence the chat trace proxy applies.  Operator aliases that no
    component declares are returned under a synthetic
    :data:`OPERATOR_COMPONENT_ID` entry so they are not silently dropped.

    Only authenticated fleet-internal consumers may call this endpoint.
    """
    records = await store.list_all()
    record_by_name: dict[str, ServiceState] = {r.name: r.state for r in records}

    operator_projects = _operator_projects(config)
    operator_openrouter = _operator_openrouter_keys(config)
    claimed: set[str] = set()
    components: list[FleetLangfuseComponent] = []

    for comp in registry.all():
        state = record_by_name.get(comp.container_name, ServiceState.UNKNOWN)

        # Try to read the component's standardized config for Langfuse keys.
        current = await read_component_config(backend, comp)
        if current:
            host, projects = _extract_langfuse_projects(current)
        else:
            host, projects = None, []

        # Operator config wins on alias collision (pinned / rotated keys).
        for p in projects:
            claimed.add(p.alias)
        projects = [operator_projects.get(p.alias, p) for p in projects]
        projects = _with_openrouter(projects, operator_openrouter)

        components.append(
            FleetLangfuseComponent(
                component_id=comp.id,
                name=comp.container_name,
                status=state.value,
                langfuse_host=host or (config.langfuse_base_url or None),
                projects=projects,
            )
        )

    unclaimed = [p for alias, p in operator_projects.items() if alias not in claimed]
    if unclaimed:
        components.append(
            FleetLangfuseComponent(
                component_id=OPERATOR_COMPONENT_ID,
                name=OPERATOR_COMPONENT_ID,
                status=ServiceState.UNKNOWN.value,
                langfuse_host=config.langfuse_base_url or None,
                projects=_with_openrouter(unclaimed, operator_openrouter),
            )
        )

    return FleetLangfuseResponse(components=components)
