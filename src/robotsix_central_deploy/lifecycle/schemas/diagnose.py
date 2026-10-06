"""Diagnose endpoint request / response schemas for lifecycle endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DiagnoseStoredSpec(BaseModel):
    """Stored component specification summary."""

    image: str = Field(description="Container image reference")
    ports: list[dict[str, Any]] = Field(
        default_factory=list, description="Port mappings (host, container, protocol)"
    )
    health_check: dict[str, Any] | None = Field(
        default=None, description="Health check config or None"
    )
    routable: bool = Field(description="Whether the component is edge-routable")
    mounts_summary: str = Field(
        description="Human-readable mount count, e.g. '3 mounts'"
    )
    git_url: str = Field(default="", description="Source repository URL")


class DiagnoseRepoContract(BaseModel):
    """Repo contract fetched from deploy/docker-compose.yml at HEAD."""

    fetched: bool = Field(description="Whether the fetch succeeded")
    error: str | None = Field(
        default=None, description="Fetch or parse error (verbatim)"
    )
    parsed_ports: list[dict[str, Any]] = Field(
        default_factory=list, description="Ports from the repo contract"
    )
    parsed_health_check: dict[str, Any] | None = Field(
        default=None, description="Health check from the repo contract"
    )
    changed_fields: list[str] = Field(
        default_factory=list,
        description="Contract-derived fields that differ from the stored spec",
    )
    previous: dict[str, Any] = Field(
        default_factory=dict, description="Previous values of changed fields"
    )
    current: dict[str, Any] = Field(
        default_factory=dict, description="New values of changed fields"
    )


class DiagnoseRouting(BaseModel):
    """Routing label comparison: expected vs actual."""

    expected_labels: dict[str, str] = Field(
        default_factory=dict,
        description="Labels traefik_labels() would generate from the stored spec",
    )
    actual_labels: dict[str, str] = Field(
        default_factory=dict,
        description="Traefik labels actually on the running container",
    )
    container_networks: dict[str, Any] = Field(
        default_factory=dict,
        description="Networks the container is attached to (name → aliases/ip)",
    )
    proxy_network_attached: bool = Field(
        description="Whether central-deploy-proxy network is attached"
    )


class DiagnoseEdgeProbe(BaseModel):
    """Edge probe result: HTTP status from inside the control plane."""

    url: str = Field(description="URL probed")
    status_code: int | None = Field(
        default=None, description="HTTP status code (None on connection error)"
    )
    body_preview: str = Field(
        default="", description="First 100 bytes of the response body"
    )
    error: str | None = Field(
        default=None, description="Connection or DNS error (verbatim)"
    )


class DiagnoseRuntime(BaseModel):
    """Runtime state from the Docker container."""

    container_state: str = Field(default="", description="Docker container state")
    health: str = Field(default="", description="Health check status")
    started_at: str = Field(default="", description="Container start time (ISO 8601)")
    restart_count: int = Field(default=0, description="Number of container restarts")
    image_digest: str = Field(
        default="", description="sha256 digest of the running image"
    )
    registry_digest: str = Field(
        default="", description="Latest digest from the registry"
    )
    recent_logs: str = Field(
        default="", description="Last 20 non-access log lines from the container"
    )


class DiagnoseVerdict(BaseModel):
    """Rule-based classification of the component's health."""

    classification: str = Field(
        description=(
            "One of: stale-contract, not-routable, no-proxy-network, "
            "unhealthy, edge-mismatch, ok"
        )
    )
    detail: str = Field(description="Human-readable explanation")
    remediation: str = Field(description="Suggested action to fix the issue")


class DiagnoseReport(BaseModel):
    """Full diagnostic report for a managed component."""

    name: str = Field(description="Component id")
    stored_spec: DiagnoseStoredSpec
    repo_contract: DiagnoseRepoContract | None = Field(
        default=None,
        description="Repo contract info (None when component has no git_url)",
    )
    routing: DiagnoseRouting
    edge_probe: DiagnoseEdgeProbe | None = Field(
        default=None, description="Edge probe result (None when not routable)"
    )
    runtime: DiagnoseRuntime | None = Field(
        default=None, description="Runtime info (None when container not found)"
    )
    verdict: DiagnoseVerdict
