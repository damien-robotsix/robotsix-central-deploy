"""FastAPI dependency providers — ``_get_*`` factories used by router endpoints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..models import HealthStatus, ServiceRecord
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.chat_agent_audit_store import ChatAgentAuditStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry import ComponentConfig, ServiceConfig
from ...registry_check import RegistryChecker
from .jobs import JobRegistry

if TYPE_CHECKING:
    from ..models import ContainerHealthSummary

logger = logging.getLogger(__name__)


async def _get_store(request: Request) -> ServiceStore:
    store = request.app.state.store
    assert store is not None, "store not initialised"
    return store  # type: ignore[no-any-return]


async def _get_backend(request: Request) -> ExecutionBackend:
    backend = request.app.state.backend
    assert backend is not None, "backend not initialised"
    return backend  # type: ignore[no-any-return]


async def _get_config(request: Request) -> LifecycleConfig:
    config = request.app.state.config
    assert config is not None, "config not initialised"
    return config  # type: ignore[no-any-return]


async def _get_registry(request: Request) -> ComponentRegistry:
    """Return the ComponentRegistry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


def _get_registry_checker(request: Request) -> RegistryChecker:
    return request.app.state.registry_checker  # type: ignore[no-any-return]


async def _get_component_config_store(request: Request) -> ComponentConfigStore:
    return request.app.state.component_config_store  # type: ignore[no-any-return]


async def _get_env_store(request: Request) -> EnvStore:
    return request.app.state.env_store  # type: ignore[no-any-return]


async def _get_config_yaml_store(request: Request) -> ConfigYamlStore:
    return request.app.state.config_yaml_store  # type: ignore[no-any-return]


async def _get_deploy_history_store(request: Request) -> DeployHistoryStore:
    return request.app.state.deploy_history_store  # type: ignore[no-any-return]


async def _get_chat_agent_audit_store(request: Request) -> ChatAgentAuditStore:
    return request.app.state.chat_agent_audit_store  # type: ignore[no-any-return]


async def _get_job_registry(request: Request) -> JobRegistry:
    return request.app.state.job_registry  # type: ignore[no-any-return]


async def _get_or_create_record(name: str, store: ServiceStore) -> ServiceRecord:
    """Fetch a service record by name, raising 404 when absent."""
    record = await store.get(name)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{name}' not found",
        )
    return record


async def _get_sibling_pairs(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
) -> list[tuple[ServiceConfig, ServiceRecord]]:
    """Return (ServiceConfig, ServiceRecord) pairs for siblings of `name`.
    Missing sibling records are logged and skipped (best-effort).
    """
    pairs = []
    for sib in config.siblings:
        sib_name = f"{name}-{sib.service_key}"
        sib_record = await store.get(sib_name)
        if sib_record is None:
            logger.warning(
                "sibling record '%s' not found; skipping",
                sib_name.replace("\n", "\\n"),
            )
            continue
        pairs.append((sib, sib_record))
    return pairs


def _compute_overall_health(
    primary_health: str,
    siblings: list["ContainerHealthSummary"],
) -> str:
    """Rollup health across primary + healthchecked siblings.

    Containers without a Docker healthcheck report health='' and are
    treated as neutral (excluded from the rollup).
    Returns '' when no container has a healthcheck configured.
    """
    candidates = [primary_health] + [s.health for s in siblings]
    checked = [h for h in candidates if h]  # non-empty → has healthcheck
    if not checked:
        return ""
    if any(h == HealthStatus.UNHEALTHY for h in checked):
        return HealthStatus.UNHEALTHY
    if any(h == HealthStatus.STARTING for h in checked):
        return HealthStatus.STARTING
    if all(h == HealthStatus.HEALTHY for h in checked):
        return HealthStatus.HEALTHY
    return ""
