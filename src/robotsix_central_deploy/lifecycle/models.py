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

    name: str  # sibling service name (e.g. "mail-ingester")
    health: str = ""  # HealthStatus value or "" (no healthcheck)
    state: ServiceState = ServiceState.UNKNOWN


class SiblingUpdateSummary(BaseModel):
    """Per-sibling update-state snapshot so the UI can aggregate the group badge."""

    name: str
    update_state: UpdateState = UpdateState.UNKNOWN


class ServiceStatus(BaseModel):
    """Full status returned by ``GET /services/{name}``."""

    name: str
    state: ServiceState
    image: str = ""
    image_revision: str = ""
    health: str = ""
    last_error: Optional[str] = None
    updated_at: float = Field(default_factory=time.time)
    update_available: bool = False
    running_digest: str = ""  # deployed_image_digest short-form (full sha256)
    latest_digest: str = ""  # last known registry manifest digest
    update_state: UpdateState = UpdateState.UNKNOWN
    has_config_yaml: bool = False
    sibling_health: list[ContainerHealthSummary] = []
    sibling_update_states: list[SiblingUpdateSummary] = []
    overall_health: str = ""  # rollup: HealthStatus value or ""


class ServiceListItem(BaseModel):
    """Summary entry returned by ``GET /services``."""

    name: str
    state: ServiceState
    update_available: bool = False
    has_config_yaml: bool = False
    component_id: str = (
        ""  # non-empty for sibling records; equals the primary service name
    )


class ServiceListResponse(BaseModel):
    """Wrapper for the service list endpoint."""

    services: list[ServiceListItem]


class ActionResponse(BaseModel):
    """Generic response for start / stop / restart requests."""

    name: str
    action: ActionType
    previous_state: ServiceState
    current_state: ServiceState
    detail: str = ""


class ErrorDetail(BaseModel):
    """Structured error response body."""

    error: str
    detail: Any = ""


class ServiceHealthResponse(BaseModel):
    """Response for ``GET /services/{name}/health``."""

    name: str
    health: str  # HealthStatus value


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

    image: Optional[str] = (
        None  # override image ref; if None, uses ComponentConfig.image
    )


class RollbackRequest(BaseModel):
    """Optional body for ``POST /services/{name}/rollback``.

    When *digest* is absent/None the current one-step rollback behaviour
    (swap deployed ↔ previous) is preserved.
    """

    digest: Optional[str] = None


class RollbackResponse(BaseModel):
    """API response for ``POST /services/{name}/rollback``."""

    name: str
    action: ActionType = ActionType.ROLLBACK
    rolled_back_to_digest: str
    current_state: ServiceState
    warnings: list[str] = []


class DeployHistoryEntry(BaseModel):
    """One entry in a component's deploy-history ledger.

    Recorded on every successful deploy (manual, caretaker, or rollback).
    """

    digest: str  # resolved sha256:... of the deployed image
    image_ref: str  # the ref actually deployed (tag or digest pin)
    timestamp: float  # unix seconds (time.time())
    source: DeploySource
    previous_digest: str = ""  # sha256 that was running before this deploy


class DeployHistoryResponse(BaseModel):
    """Response for ``GET /services/{name}/history`` — most-recent-first."""

    name: str
    entries: list[DeployHistoryEntry] = []


# ---------------------------------------------------------------------------
# Disk usage schemas
# ---------------------------------------------------------------------------


class VolumeStat(BaseModel):
    """Size of a single Docker volume (from ``docker system df``)."""

    name: str
    size_bytes: int = 0
    in_use: bool = False


class DockerDfStats(BaseModel):
    """Docker storage breakdown from ``docker system df``."""

    images_size_bytes: int = 0
    dangling_images_bytes: int = 0
    build_cache_size_bytes: int = 0
    build_cache_reclaimable_bytes: int = 0
    volumes: list[VolumeStat] = []


class DiskUsageResponse(BaseModel):
    """Host disk usage + Docker storage breakdown."""

    total_bytes: int
    used_bytes: int
    free_bytes: int
    warn_threshold_pct: float
    docker: DockerDfStats


class ReclaimResponse(BaseModel):
    """Result of a build-cache reclaim operation."""

    space_reclaimed_bytes: int


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

    supported: bool
    container_name: str = ""
    image: str = ""
    running_digest: str = ""
    latest_digest: str = ""
    update_available: bool = False


class SelfUpdateTriggered(BaseModel):
    """Response model for ``POST /system/update``."""

    status: str = "update-started"
    updater_container_id: str
