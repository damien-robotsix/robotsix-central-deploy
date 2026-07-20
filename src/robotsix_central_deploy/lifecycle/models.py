"""Service model, state machine, and API schemas for the lifecycle server."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------


class ServiceState(str, Enum):
    """The seven lifecycle states a managed service can be in."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    RESTARTING = "restarting"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HealthStatus(str, Enum):
    """Container health status — mirrors Docker health-check states."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    UNKNOWN = "unknown"


class UpdateState(str, Enum):
    """Image-update availability derived from registry polling."""

    UNKNOWN = "unknown"
    UP_TO_DATE = "up-to-date"
    UPDATE_AVAILABLE = "update-available"


class StoreBackend(str, Enum):
    """Persistence backend for the lifecycle service store."""

    MEMORY = "memory"
    FILE = "file"


class ExecutionBackendType(str, Enum):
    """Execution backend for container lifecycle operations."""

    DOCKER_SDK = "docker_sdk"
    DOCKER = "docker"
    NOOP = "noop"


class VolumeEntryType(str, Enum):
    """Filesystem entry type returned by volume directory listing."""

    FILE = "file"
    DIR = "dir"


class ActionType(str, Enum):
    """Action type for start / stop / restart / rollback requests."""

    START = "start"
    STOP = "stop"
    RESTART = "restart"
    ROLLBACK = "rollback"


class DeploySource(str, Enum):
    """Source of a deploy: manual operator action, caretaker auto-update, or rollback."""

    MANUAL = "manual"
    CARETAKER = "caretaker"
    ROLLBACK = "rollback"


class OnboardJobPhase(str, Enum):
    """Phases of a background onboard deploy job."""

    WRITING_CONFIG = "writing_config"
    DEPLOYING_PRIMARY = "deploying_primary"
    WAITING_HEALTH = "waiting_health"
    DEPLOYING_SIBLINGS = "deploying_siblings"
    DONE = "done"
    FAILED = "failed"


class DeployJobPhase(str, Enum):
    """Phases of a background deploy job."""

    DEPLOYING = "deploying"
    WAITING_HEALTH = "waiting_health"
    DEPLOYING_SIBLINGS = "deploying_siblings"
    DONE = "done"
    FAILED = "failed"


#: Allowed transitions: state → set of reachable next states.
TRANSITIONS: dict[ServiceState, set[ServiceState]] = {
    ServiceState.STOPPED: {ServiceState.STARTING},
    ServiceState.STARTING: {ServiceState.RUNNING, ServiceState.FAILED},
    ServiceState.RUNNING: {ServiceState.STOPPING, ServiceState.RESTARTING},
    ServiceState.STOPPING: {ServiceState.STOPPED, ServiceState.FAILED},
    ServiceState.RESTARTING: {ServiceState.STOPPING},
    ServiceState.FAILED: {ServiceState.STARTING},
    ServiceState.UNKNOWN: {ServiceState.STARTING, ServiceState.STOPPING},
}


def can_transition(current: ServiceState, target: ServiceState) -> bool:
    """Return *True* if the state machine allows *current → target*."""
    return target in TRANSITIONS.get(current, set())


#: Name of the Docker named volume that holds Claude OAuth credentials.
CLAUDE_AUTH_VOLUME: str = "claude-auth"


# ---------------------------------------------------------------------------
# Domain record (not a Pydantic model — used internally)
# ---------------------------------------------------------------------------


@dataclass
class ComponentInspect:
    """Rich status returned by ``ExecutionBackend.status()``."""

    state: ServiceState
    image_revision: str = ""  # org.opencontainers.image.revision label; empty if absent
    health: str = ""  # HealthStatus value or "" (no health check)
    running_digest: str = (
        ""  # sha256:... from image RepoDigests; empty when unresolvable
    )


@dataclass
class ServiceRecord:
    """Internal representation of a managed service."""

    name: str
    image: str = ""
    state: ServiceState = ServiceState.UNKNOWN
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)
    container_name: str = ""  # Docker container name; if blank, falls back to `name`
    image_revision: str = ""
    health: str = ""
    deployed_image_digest: str = ""  # sha256 digest of the currently running image
    previous_image_digest: str = (
        ""  # sha256 digest of the image before the last deploy (enables rollback)
    )
    update_available: bool = False
    latest_registry_digest: str = ""
    component_id: str = (
        ""  # non-empty for sibling records; set to primary component name
    )
    repo_id: str = ""

    def to_status(self) -> "ServiceStatus":
        if not self.deployed_image_digest or not self.latest_registry_digest:
            update_state = UpdateState.UNKNOWN
        elif self.deployed_image_digest == self.latest_registry_digest:
            update_state = UpdateState.UP_TO_DATE
        else:
            update_state = UpdateState.UPDATE_AVAILABLE
        return ServiceStatus(
            name=self.name,
            state=self.state,
            image=self.image,
            last_error=self.last_error or None,
            updated_at=self.updated_at,
            image_revision=self.image_revision,
            health=self.health,
            running_digest=self.deployed_image_digest,
            latest_digest=self.latest_registry_digest,
            update_available=(update_state == UpdateState.UPDATE_AVAILABLE),
            update_state=update_state,
        )

    def to_list_item(self) -> "ServiceListItem":
        return ServiceListItem(
            name=self.name,
            state=self.state,
            update_available=self.update_available,
            component_id=self.component_id,
        )


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class ContainerHealthSummary(BaseModel):
    """Health snapshot for one sibling container."""

    name: str = Field(description="Sibling service name (e.g. 'mail-ingester')")
    health: str = Field(
        "",
        description="HealthStatus value, or empty string if the container has no healthcheck configured",
    )
    state: ServiceState = Field(
        ServiceState.UNKNOWN,
        description="Current lifecycle state of this sibling container",
    )


class SiblingUpdateSummary(BaseModel):
    """Per-sibling update-state snapshot so the UI can aggregate the group badge."""

    name: str = Field(description="Sibling service name")
    update_state: UpdateState = Field(
        UpdateState.UNKNOWN,
        description="Image-update availability for this sibling",
    )


class ServiceStatus(BaseModel):
    """Full status returned by ``GET /services/{name}``."""

    name: str = Field(description="Component name")
    state: ServiceState = Field(description="Current lifecycle state")
    image: str = Field(
        "",
        description="Docker image reference (e.g. 'ghcr.io/org/service:main')",
    )
    image_revision: str = Field(
        "",
        description="VCS revision label from the running image (org.opencontainers.image.revision), empty if absent",
    )
    health: str = Field(
        "",
        description="Container health status value; empty string if no healthcheck is defined",
    )
    last_error: Optional[str] = Field(
        None,
        description="Most recent error message; None when the service is healthy",
    )
    updated_at: float = Field(
        default_factory=time.time,
        description="Unix timestamp (seconds) of the last status update",
    )
    update_available: bool = Field(
        False,
        description="True when a newer image digest is available on the registry",
    )
    running_digest: str = Field(
        "",
        description="SHA256 digest of the currently deployed image (full sha256:...)",
    )
    latest_digest: str = Field(
        "",
        description="SHA256 digest of the latest manifest known to the registry checker",
    )
    update_state: UpdateState = Field(
        UpdateState.UNKNOWN,
        description="Enum indicating whether an image update is available",
    )
    has_config_yaml: bool = Field(
        False,
        description="True when this component has an operator-supplied config.yaml",
    )
    sibling_health: list[ContainerHealthSummary] = Field(
        default_factory=list,
        description="Per-sibling container health snapshots for multi-service components",
    )
    sibling_update_states: list[SiblingUpdateSummary] = Field(
        default_factory=list,
        description="Per-sibling update-state snapshots for the UI aggregate badge",
    )
    overall_health: str = Field(
        "",
        description="Rollup health status across primary + siblings; HealthStatus value or empty string",
    )


class ServiceListItem(BaseModel):
    """Summary entry returned by ``GET /services``."""

    name: str = Field(description="Component name")
    state: ServiceState = Field(description="Current lifecycle state")
    update_available: bool = Field(
        False,
        description="True when a newer image digest is available on the registry",
    )
    has_config_yaml: bool = Field(
        False,
        description="True when this component has an operator-supplied config.yaml",
    )
    component_id: str = Field(
        "",
        description="Non-empty for sibling records; set to the primary component name",
    )


class ServiceListResponse(BaseModel):
    """Wrapper for the service list endpoint."""

    services: list[ServiceListItem] = Field(
        default_factory=list,
        description="List of managed service summaries",
    )


class ActionResponse(BaseModel):
    """Generic response for start / stop / restart requests."""

    name: str = Field(description="Component name")
    action: ActionType = Field(
        description="Action type: start, stop, restart, or rollback"
    )
    previous_state: ServiceState = Field(
        description="Lifecycle state before the action was applied"
    )
    current_state: ServiceState = Field(
        description="Lifecycle state after the action completed"
    )
    detail: str = Field(
        "",
        description="Human-readable detail message; empty string when no extra context is available",
    )


class ErrorDetail(BaseModel):
    """Structured error response body."""

    error: str = Field(description="Short error label")
    detail: Any = Field(
        "", description="Arbitrary structured detail (dict, list, string, or None)"
    )


class ServiceHealthResponse(BaseModel):
    """Response for ``GET /services/{name}/health``."""

    name: str = Field(description="Component name")
    health: str = Field(description="HealthStatus value for the primary container")


# ---------------------------------------------------------------------------
# Deploy / rollback schemas
# ---------------------------------------------------------------------------


@dataclass
class DeployOutcome:
    """Result of a ``deploy()`` call on the execution backend."""

    deployed_digest: str  # sha256 digest of the newly pulled/started image
    previous_digest: str  # sha256 digest of the image that was running before
    state: ServiceState
    warnings: list[str] = field(default_factory=list)


@dataclass
class RollbackOutcome:
    """Result of a ``rollback()`` call on the execution backend."""

    deployed_digest: str  # sha256 digest of the image now running (the prior digest)
    state: ServiceState
    warnings: list[str] = field(default_factory=list)


class DeployRequest(BaseModel):
    """Optional image override for a deploy request."""

    image: Optional[str] = Field(
        None,
        description="Override Docker image reference; when None the stored ComponentConfig.image is used",
    )


class RollbackRequest(BaseModel):
    """Optional body for ``POST /services/{name}/rollback``.

    When *digest* is absent/None the current one-step rollback behaviour
    (swap deployed ↔ previous) is preserved.
    """

    digest: Optional[str] = Field(
        None,
        description="Target digest to roll back to; when None the previous digest is used",
    )


class RollbackResponse(BaseModel):
    """API response for ``POST /services/{name}/rollback``."""

    name: str = Field(description="Component name")
    action: ActionType = Field(
        ActionType.ROLLBACK,
        description="Always ActionType.ROLLBACK",
    )
    rolled_back_to_digest: str = Field(
        description="SHA256 digest of the image now running after rollback"
    )
    current_state: ServiceState = Field(
        description="Lifecycle state after rollback completed"
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings collected during the rollback",
    )


class DeployHistoryEntry(BaseModel):
    """One entry in a component's deploy-history ledger.

    Recorded on every successful deploy (manual, caretaker, or rollback).
    """

    digest: str = Field(description="Resolved SHA256 digest of the deployed image")
    image_ref: str = Field(
        description="The Docker image reference actually deployed (tag or digest pin)"
    )
    timestamp: float = Field(
        description="Unix timestamp (seconds) when the deploy completed"
    )
    source: DeploySource = Field(
        description="What triggered the deploy: manual, caretaker auto-update, or rollback"
    )
    previous_digest: str = Field(
        "",
        description="SHA256 digest that was running before this deploy; empty for initial deploy",
    )


class DeployHistoryResponse(BaseModel):
    """Response for ``GET /services/{name}/history`` — most-recent-first."""

    name: str = Field(description="Component name")
    entries: list[DeployHistoryEntry] = Field(
        default_factory=list,
        description="Deploy history entries ordered most-recent-first",
    )


# ---------------------------------------------------------------------------
# Disk usage schemas
# ---------------------------------------------------------------------------


class VolumeStat(BaseModel):
    """Size of a single Docker volume (from ``docker system df``)."""

    name: str = Field(description="Docker volume name")
    size_bytes: int = Field(
        0,
        description="Storage consumed by this volume in bytes",
    )
    in_use: bool = Field(
        False,
        description="True when the volume is attached to at least one container",
    )


class DockerDfStats(BaseModel):
    """Docker storage breakdown from ``docker system df``."""

    images_size_bytes: int = Field(
        0,
        description="Total storage consumed by all Docker images",
    )
    dangling_images_bytes: int = Field(
        0,
        description="Storage consumed by dangling (untagged) images",
    )
    build_cache_size_bytes: int = Field(
        0,
        description="Total build cache storage",
    )
    build_cache_reclaimable_bytes: int = Field(
        0,
        description="Build cache storage eligible for reclamation",
    )
    volumes: list[VolumeStat] = Field(
        default_factory=list,
        description="Per-volume size breakdown",
    )


class DiskUsageResponse(BaseModel):
    """Host disk usage + Docker storage breakdown."""

    total_bytes: int = Field(description="Total host disk capacity in bytes")
    used_bytes: int = Field(description="Used host disk space in bytes")
    free_bytes: int = Field(description="Free host disk space in bytes")
    warn_threshold_pct: float = Field(
        description="Percentage threshold at which the UI shows a disk-space warning"
    )
    docker: DockerDfStats = Field(description="Docker storage breakdown")


class ReclaimResponse(BaseModel):
    """Result of a build-cache reclaim operation."""

    space_reclaimed_bytes: int = Field(
        description="Bytes freed by the build-cache reclaim operation"
    )


@dataclass
class SelfInspect:
    """What ``ExecutionBackend.inspect_self()`` learns about the server's own container."""

    container_id: str
    container_name: str
    image_ref: str  # e.g. ghcr.io/damien-robotsix/robotsix-central-deploy:main
    running_digest: str = ""  # sha256:... from image RepoDigests; "" when unresolvable
    networks: list[str] = field(default_factory=list)  # attached network names


class SelfUpdateStatus(BaseModel):
    """Response model for ``GET /system/update``."""

    supported: bool = Field(
        description="True when the server is running inside a Docker container"
    )
    container_name: str = Field(
        "",
        description="Name of the server's own container; empty when not in a container",
    )
    image: str = Field(
        "",
        description="Docker image reference for the server",
    )
    running_digest: str = Field(
        "",
        description="SHA256 digest of the currently running server image",
    )
    latest_digest: str = Field(
        "",
        description="SHA256 digest of the latest server image on the registry",
    )
    update_available: bool = Field(
        False,
        description="True when a newer server image is available",
    )


class SelfUpdateTriggered(BaseModel):
    """Response model for ``POST /system/update``."""

    status: str = Field(
        "update-started",
        description="Always 'update-started'",
    )
    updater_container_id: str = Field(
        description="Docker container ID of the one-shot watchtower updater"
    )
