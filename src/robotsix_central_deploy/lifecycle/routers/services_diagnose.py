"""Diagnostic endpoint for managed services.

``GET /services/{name}/diagnose`` returns a structured report comparing
stored spec, repo contract, routing labels, edge reachability, and
container runtime state.  Read-only, no side effects.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from ...registry_check import RegistryChecker
from .._diagnose import build_diagnose_report
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_registry,
    _get_registry_checker,
    _get_store,
)
from ..models import ErrorDetail
from ..schemas import DiagnoseReport
from ..store import ServiceStore

router = APIRouter(tags=["services"])


@router.get(
    "/services/{name}/diagnose",
    response_model=DiagnoseReport,
    summary="Diagnostic report for a managed service",
    responses={
        404: {"model": ErrorDetail, "description": "Service not found"},
    },
)
async def diagnose_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    lifecycle_config: LifecycleConfig = Depends(_get_config),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> DiagnoseReport:
    """Return a structured diagnostic report for a managed component.

    Compares stored spec vs repo contract, expected vs actual Traefik
    labels, edge reachability, and container runtime state.  Every
    section is best-effort — a failure in one section does not prevent
    the others from being populated.

    Read-only: no side effects, no state mutations.
    """
    config = component_config_store.get(name)
    if config is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    checker: RegistryChecker = _get_registry_checker(request)

    return await build_diagnose_report(
        name=name,
        config=config,
        store=store,
        backend=backend,
        component_config_store=component_config_store,
        lifecycle_config=lifecycle_config,
        registry=registry,
        checker=checker,
    )
