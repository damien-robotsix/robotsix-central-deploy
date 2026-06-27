"""Service model, state machine, and API schemas for the lifecycle server."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# State machine
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


#: Allowed transitions: state → set of reachable next states.
TRANSITIONS: dict[ServiceState, set[ServiceState]] = {
    ServiceState.STOPPED:    {ServiceState.STARTING},
    ServiceState.STARTING:   {ServiceState.RUNNING, ServiceState.FAILED},
    ServiceState.RUNNING:    {ServiceState.STOPPING, ServiceState.RESTARTING},
    ServiceState.STOPPING:   {ServiceState.STOPPED, ServiceState.FAILED},
    ServiceState.RESTARTING: {ServiceState.STOPPING},
    ServiceState.FAILED:     {ServiceState.STARTING},
    ServiceState.UNKNOWN:    {ServiceState.STARTING, ServiceState.STOPPING},
}

#: States that are considered "in-flight" (user-requested transition not yet settled).
ACTIVE_STATES: set[ServiceState] = {
    ServiceState.STARTING,
    ServiceState.STOPPING,
    ServiceState.RESTARTING,
}

#: States that are "at rest" — a subsequent start/stop produces a clean transition.
RESTING_STATES: set[ServiceState] = {
    ServiceState.STOPPED,
    ServiceState.RUNNING,
    ServiceState.FAILED,
    ServiceState.UNKNOWN,
}


def can_transition(current: ServiceState, target: ServiceState) -> bool:
    """Return *True* if the state machine allows *current → target*."""
    return target in TRANSITIONS.get(current, set())


def is_active(current: ServiceState) -> bool:
    """Return *True* when a service is mid-transition."""
    return current in ACTIVE_STATES


# ---------------------------------------------------------------------------
# Domain record (not a Pydantic model — used internally)
# ---------------------------------------------------------------------------


@dataclass
class ComponentInspect:
    """Rich status returned by ``ExecutionBackend.status()``."""

    state: ServiceState
    image_revision: str = ""  # org.opencontainers.image.revision label; empty if absent
    health: str = ""          # "healthy" | "unhealthy" | "starting" | "" (no health check)
    running_digest: str = ""  # sha256:... from image RepoDigests; empty when unresolvable


@dataclass
class ServiceRecord:
    """Internal representation of a managed service."""

    name: str
    image: str = ""
    state: ServiceState = ServiceState.UNKNOWN
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)
    container_name: str = ""     # Docker container name; if blank, falls back to `name`
    image_revision: str = ""
    health: str = ""
    deployed_image_digest: str = ""   # sha256 digest of the currently running image
    previous_image_digest: str = ""  # sha256 digest of the image before the last deploy (enables rollback)
    update_available: bool = False
    latest_registry_digest: str = ""
    component_id: str = ""  # non-empty for sibling records; set to primary component name

    def to_status(self) -> "ServiceStatus":
        if not self.deployed_image_digest or not self.latest_registry_digest:
            update_state: Literal["unknown", "up-to-date", "update-available"] = "unknown"
        elif self.deployed_image_digest == self.latest_registry_digest:
            update_state = "up-to-date"
        else:
            update_state = "update-available"
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
            update_available=(update_state == "update-available"),
            update_state=update_state,
        )

    def to_list_item(self) -> "ServiceListItem":
        return ServiceListItem(name=self.name, state=self.state, update_available=self.update_available)


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


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
    running_digest: str = ""   # deployed_image_digest short-form (full sha256)
    latest_digest: str = ""    # last known registry manifest digest
    update_state: Literal["unknown", "up-to-date", "update-available"] = "unknown"


class ServiceListItem(BaseModel):
    """Summary entry returned by ``GET /services``."""

    name: str
    state: ServiceState
    update_available: bool = False


class ServiceListResponse(BaseModel):
    """Wrapper for the service list endpoint."""

    services: list[ServiceListItem]


class ActionResponse(BaseModel):
    """Generic response for start / stop / restart requests."""

    name: str
    action: str  # "start" | "stop" | "restart"
    previous_state: ServiceState
    current_state: ServiceState
    detail: str = ""


class ErrorDetail(BaseModel):
    """Structured error response body."""

    error: str
    detail: str = ""


class ServiceHealthResponse(BaseModel):
    """Response for ``GET /services/{name}/health``."""

    name: str
    health: str  # "healthy" | "unhealthy" | "starting" | "unknown"


# ---------------------------------------------------------------------------
# Deploy / rollback schemas
# ---------------------------------------------------------------------------


@dataclass
class DeployOutcome:
    """Result of a ``deploy()`` call on the execution backend."""

    deployed_digest: str   # sha256 digest of the newly pulled/started image
    previous_digest: str   # sha256 digest of the image that was running before
    state: ServiceState


@dataclass
class RollbackOutcome:
    """Result of a ``rollback()`` call on the execution backend."""

    deployed_digest: str   # sha256 digest of the image now running (the prior digest)
    state: ServiceState


class DeployRequest(BaseModel):
    """Optional image override for a deploy request."""

    image: Optional[str] = None  # override image ref; if None, uses ComponentConfig.image


class DeployResponse(BaseModel):
    """API response for ``POST /services/{name}/deploy``."""

    name: str
    action: str = "deploy"
    deployed_digest: str
    previous_digest: str
    current_state: ServiceState


class RollbackResponse(BaseModel):
    """API response for ``POST /services/{name}/rollback``."""

    name: str
    action: str = "rollback"
    rolled_back_to_digest: str
    current_state: ServiceState


# ---------------------------------------------------------------------------
# Disk usage schemas
# ---------------------------------------------------------------------------


class DockerDfStats(BaseModel):
    """Docker storage breakdown from ``docker system df``."""

    images_size_bytes: int = 0
    build_cache_size_bytes: int = 0
    build_cache_reclaimable_bytes: int = 0


class DiskUsageResponse(BaseModel):
    """Host disk usage + Docker storage breakdown."""

    total_bytes: int
    used_bytes: int
    free_bytes: int
    warn_threshold_bytes: int
    docker: DockerDfStats
