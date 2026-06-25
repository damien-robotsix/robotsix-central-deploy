"""Service model, state machine, and API schemas for the lifecycle server."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

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
class ServiceRecord:
    """Internal representation of a managed service."""

    name: str
    image: str = ""
    state: ServiceState = ServiceState.UNKNOWN
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_status(self) -> "ServiceStatus":
        return ServiceStatus(
            name=self.name,
            state=self.state,
            image=self.image,
            last_error=self.last_error or None,
            updated_at=self.updated_at,
        )

    def to_list_item(self) -> "ServiceListItem":
        return ServiceListItem(name=self.name, state=self.state)


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class ServiceStatus(BaseModel):
    """Full status returned by ``GET /services/{name}``."""

    name: str
    state: ServiceState
    image: str = ""
    last_error: Optional[str] = None
    updated_at: float = Field(default_factory=time.time)


class ServiceListItem(BaseModel):
    """Summary entry returned by ``GET /services``."""

    name: str
    state: ServiceState


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
