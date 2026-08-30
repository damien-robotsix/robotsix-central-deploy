"""Core diagnostic logic for the /services/{name}/diagnose endpoint.

Builds a structured ``DiagnoseReport`` comparing stored spec, repo contract,
routing labels, edge reachability, and container runtime state.  Every
section is best-effort: a failure in one section does not prevent the
others from being populated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..registry.config_store import ComponentConfigStore
from ..registry.loader import ComponentRegistry
from ..registry.models import ComponentConfig
from ..registry.traefik_labels import traefik_labels
from ..registry_check import RegistryChecker
from .backends import ExecutionBackend
from .config import LifecycleConfig
from .deps._contract_refresh import _CONTRACT_FIELDS
from .models import ServiceRecord
from .schemas import (
    DiagnoseEdgeProbe,
    DiagnoseRepoContract,
    DiagnoseReport,
    DiagnoseRouting,
    DiagnoseRuntime,
    DiagnoseStoredSpec,
    DiagnoseVerdict,
)
from .store import ServiceStore

logger = logging.getLogger(__name__)

from ..registry.constants import PROXY_NETWORK


def _compute_verdict(
    *,
    config: ComponentConfig,
    repo_contract: DiagnoseRepoContract | None,
    routing: DiagnoseRouting,
    edge_probe: DiagnoseEdgeProbe | None,
    runtime: DiagnoseRuntime | None,
) -> DiagnoseVerdict:
    """Compute the rule-based verdict from the diagnostic sections.

    Priority (highest first):
    1. not-routable — component is not meant to be edge-routed
    2. no-proxy-network — container not on central-deploy-proxy
    3. stale-contract — repo contract differs or refresh fails
    4. unhealthy — container health check is failing
    5. edge-mismatch — expected labels differ from actual
    6. ok — no issues detected
    """
    # 1. Not routable
    if not config.routable:
        return DiagnoseVerdict(
            classification="not-routable",
            detail="Component has routable=false; it is not published at the edge.",
            remediation="No action needed — this is by design for sibling/internal services.",
        )

    # 2. No proxy network
    if runtime is not None and not routing.proxy_network_attached:
        return DiagnoseVerdict(
            classification="no-proxy-network",
            detail="Container is running but not attached to the central-deploy-proxy network.",
            remediation=f"Recreate the container: POST /services/{config.id}/deploy",
        )

    # 3. Stale contract
    if repo_contract is not None:
        if repo_contract.error:
            return DiagnoseVerdict(
                classification="stale-contract",
                detail=f"Contract refresh failed: {repo_contract.error}",
                remediation=f"POST /services/{config.id}/refresh-contract then deploy",
            )
        if repo_contract.changed_fields:
            return DiagnoseVerdict(
                classification="stale-contract",
                detail=f"Stored spec differs from repo contract in: {', '.join(repo_contract.changed_fields)}",
                remediation=f"POST /services/{config.id}/refresh-contract then deploy",
            )

    # 4. Unhealthy
    if runtime is not None and runtime.health == "unhealthy":
        return DiagnoseVerdict(
            classification="unhealthy",
            detail="Container health check is reporting unhealthy.",
            remediation=f"Check logs: GET /services/{config.id}/logs?tail=50",
        )

    # 5. Edge mismatch
    if routing.expected_labels and routing.actual_labels:
        # Compare only traefik.* labels
        expected_traefik = {
            k: v for k, v in routing.expected_labels.items() if k.startswith("traefik.")
        }
        actual_traefik = {
            k: v for k, v in routing.actual_labels.items() if k.startswith("traefik.")
        }
        if expected_traefik != actual_traefik:
            return DiagnoseVerdict(
                classification="edge-mismatch",
                detail="Traefik labels on the container differ from what the stored spec would generate.",
                remediation=f"Recreate the container: POST /services/{config.id}/deploy",
            )

    # 6. Edge probe failure (when we have labels but edge returns non-2xx)
    if (
        edge_probe is not None
        and edge_probe.status_code is not None
        and edge_probe.status_code >= 400
    ):
        return DiagnoseVerdict(
            classification="edge-mismatch",
            detail=f"Edge probe returned HTTP {edge_probe.status_code}.",
            remediation=f"Check Traefik routing and container health: GET /services/{config.id}/diagnose",
        )

    # 7. OK
    return DiagnoseVerdict(
        classification="ok",
        detail="No issues detected.",
        remediation="No action needed.",
    )


async def build_diagnose_report(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    lifecycle_config: LifecycleConfig,
    registry: ComponentRegistry,
    checker: RegistryChecker | None,
) -> DiagnoseReport:
    """Build a full diagnostic report for a component.

    Every section is best-effort: failures are captured in the relevant
    section's error field rather than aborting the whole report.
    """
    # ── Stored spec ──────────────────────────────────────────────────────
    mounts_summary = (
        f"{len(config.mounts)} mount{'s' if len(config.mounts) != 1 else ''}"
    )
    stored_spec = DiagnoseStoredSpec(
        image=config.image,
        ports=[p.model_dump() for p in config.ports],
        health_check=config.health_check.model_dump() if config.health_check else None,
        routable=config.routable,
        mounts_summary=mounts_summary,
        git_url=config.git_url,
    )

    # ── Repo contract ────────────────────────────────────────────────────
    repo_contract: DiagnoseRepoContract | None = None
    if config.git_url:
        repo_contract = await _fetch_and_compare_contract(
            name, config, component_config_store, lifecycle_config, registry
        )

    # ── Routing ──────────────────────────────────────────────────────────
    base_domain = lifecycle_config.gateway_base_domain
    expected_labels = traefik_labels(config, base_domain, PROXY_NETWORK)

    container_diag = await backend.get_container_diagnostics(
        ServiceRecord(name=name, container_name=config.container_name or name)
    )
    actual_labels: dict[str, str] = {}
    container_networks: dict[str, Any] = {}
    proxy_network_attached = False
    if container_diag.get("exists"):
        actual_labels = {
            k: v
            for k, v in container_diag.get("labels", {}).items()
            if k.startswith("traefik.")
        }
        container_networks = container_diag.get("networks", {})
        proxy_network_attached = PROXY_NETWORK in container_networks

    routing = DiagnoseRouting(
        expected_labels=expected_labels,
        actual_labels=actual_labels,
        container_networks=container_networks,
        proxy_network_attached=proxy_network_attached,
    )

    # ── Edge probe ───────────────────────────────────────────────────────
    edge_probe: DiagnoseEdgeProbe | None = None
    if config.routable and base_domain and config.ports:
        edge_probe = await _probe_edge(name, base_domain)

    # ── Runtime ──────────────────────────────────────────────────────────
    runtime: DiagnoseRuntime | None = None
    if container_diag.get("exists"):
        registry_digest = ""
        if checker:
            try:
                digest = await checker.get_latest_digest(config.image)
                if digest:
                    registry_digest = digest
            except Exception:  # noqa: BLE001
                logger.debug("registry digest lookup failed for %s", config.image)

        recent_logs = ""
        try:
            record = ServiceRecord(
                name=name, container_name=config.container_name or name
            )
            recent_logs = await backend.get_container_logs(record, tail=20)
        except Exception:  # noqa: BLE001
            logger.debug("log capture failed for %s", name)

        runtime = DiagnoseRuntime(
            container_state=container_diag.get("state", ""),
            health=container_diag.get("health", ""),
            started_at=container_diag.get("started_at", ""),
            restart_count=container_diag.get("restart_count", 0),
            image_digest=container_diag.get("image_digest", ""),
            registry_digest=registry_digest,
            recent_logs=recent_logs,
        )

    # ── Verdict ──────────────────────────────────────────────────────────
    verdict = _compute_verdict(
        config=config,
        repo_contract=repo_contract,
        routing=routing,
        edge_probe=edge_probe,
        runtime=runtime,
    )

    return DiagnoseReport(
        name=name,
        stored_spec=stored_spec,
        repo_contract=repo_contract,
        routing=routing,
        edge_probe=edge_probe,
        runtime=runtime,
        verdict=verdict,
    )


async def _fetch_and_compare_contract(
    name: str,
    config: ComponentConfig,
    component_config_store: ComponentConfigStore,
    lifecycle_config: LifecycleConfig,
    registry: ComponentRegistry,
) -> DiagnoseRepoContract:
    """Fetch the repo contract and compare it to the stored spec.

    Returns a ``DiagnoseRepoContract`` with the fetch result, parsed
    fields, and a diff of changed contract-derived fields.  Never raises —
    errors are captured in the ``error`` field.
    """
    from robotsix_central_deploy.onboard.parser import ParseError, parse_compose

    from .deps.seed import (
        _build_component_config_from_spec,
        _fetch_component_repo_files,
        _namespace_spec_volumes,
    )

    try:
        _comp_cfg, repo_files = await _fetch_component_repo_files(
            name, component_config_store, lifecycle_config
        )
    except Exception as exc:  # noqa: BLE001
        # _fetch_component_repo_files raises HTTPException on failure;
        # capture the detail message.
        detail = str(exc)
        if hasattr(exc, "detail"):
            detail = str(exc.detail)
        return DiagnoseRepoContract(fetched=False, error=detail)

    if repo_files.compose_bytes is None:
        return DiagnoseRepoContract(
            fetched=False,
            error="Repo has no deploy/docker-compose.yml",
        )

    loop = asyncio.get_running_loop()
    try:
        spec = await loop.run_in_executor(
            None, parse_compose, repo_files.compose_bytes, name, config.git_url
        )
    except ParseError as exc:
        return DiagnoseRepoContract(
            fetched=False,
            error=f"Parse failed: {'; '.join(exc.violations)}",
        )

    spec = _namespace_spec_volumes(spec, name)
    new_config = _build_component_config_from_spec(
        spec,
        git_url=config.git_url,
        repo_id=config.repo_id,
        caretaker_auto_update=config.caretaker_auto_update,
        mem_limit=config.mem_limit,
        allow_chat_access=config.allow_chat_access,
        claude_mount=config.claude_mount,
    )

    changed: list[str] = []
    previous: dict[str, Any] = {}
    current: dict[str, Any] = {}
    for cfg_field in _CONTRACT_FIELDS:
        old_val = getattr(config, cfg_field)
        new_val = getattr(new_config, cfg_field)
        if old_val != new_val:
            changed.append(cfg_field)
            if hasattr(old_val, "model_dump"):
                previous[cfg_field] = old_val.model_dump()
            elif (
                isinstance(old_val, list)
                and old_val
                and hasattr(old_val[0], "model_dump")
            ):
                previous[cfg_field] = [v.model_dump() for v in old_val]
            else:
                previous[cfg_field] = old_val
            if hasattr(new_val, "model_dump"):
                current[cfg_field] = new_val.model_dump()
            elif (
                isinstance(new_val, list)
                and new_val
                and hasattr(new_val[0], "model_dump")
            ):
                current[cfg_field] = [v.model_dump() for v in new_val]
            else:
                current[cfg_field] = new_val

    return DiagnoseRepoContract(
        fetched=True,
        parsed_ports=[p.model_dump() for p in new_config.ports],
        parsed_health_check=(
            new_config.health_check.model_dump() if new_config.health_check else None
        ),
        changed_fields=changed,
        previous=previous,
        current=current,
    )


async def _probe_edge(name: str, base_domain: str) -> DiagnoseEdgeProbe:
    """Probe the edge for a component's /health endpoint.

    Returns a ``DiagnoseEdgeProbe`` with the HTTP status code and a body
    preview.  Never raises — connection errors are captured in the
    ``error`` field.
    """
    url = f"https://{name}.{base_domain}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:  # noqa: S501
            resp = await client.get(url)
            body_preview = resp.text[:100]
            return DiagnoseEdgeProbe(
                url=url,
                status_code=resp.status_code,
                body_preview=body_preview,
            )
    except Exception as exc:  # noqa: BLE001
        return DiagnoseEdgeProbe(
            url=url,
            error=str(exc),
        )
