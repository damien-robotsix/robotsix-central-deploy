"""Onboard request / response schemas for lifecycle endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.models import OnboardJobPhase
from robotsix_central_deploy.lifecycle.schemas._base import _JobStatusFields
from robotsix_central_deploy.onboard.models import DerivedSpec


class PortShift(BaseModel):
    """A host-port remapping applied during onboarding to avoid a collision."""

    container_port: int = Field(
        description="Container-side port from the service's docker-compose.yml"
    )
    protocol: str = Field(description="Transport protocol: 'tcp' or 'udp'")
    original_host: int = Field(
        description="Host port from the repo's docker-compose.yml"
    )
    assigned_host: int = Field(
        description="Auto-assigned free host port during onboarding"
    )
    collision_component_id: str = Field(
        description="Component whose stored port collided; 'central-deploy' for lifecycle-assigned ports"
    )
    collision_repo_id: str = Field(
        description="Repo ID of the colliding component; empty string when unknown"
    )


class OnboardPreflightRequest(BaseModel):
    """Request body for POST /onboard/preflight."""

    git_url: str = Field(description="Git clone URL of the repository to onboard")
    name: str = Field(description="Component name; must match ^[a-z0-9][a-z0-9-]*$")
    target_disk: str = Field(
        default="",
        description=(
            "Target disk identifier (device path, mount point, or label) "
            "for volume placement. Empty means use the config default or "
            "Docker's default volume location."
        ),
    )


class OnboardPreflightResponse(BaseModel):
    """Derived spec and port shifts returned by POST /onboard/preflight."""

    spec: DerivedSpec = Field(
        description="Derived deployment specification for the component"
    )
    port_shifts: list[PortShift] = Field(
        default=[],
        description="Port remappings needed to avoid collisions; empty when no shifts required",
    )


class OnboardConfirmRequest(BaseModel):
    """Request body for POST /onboard/confirm — the user-approved spec."""

    spec: DerivedSpec = Field(
        description="Final DerivedSpec with user-supplied environment values"
    )
    config_values: dict[str, Any] | None = Field(
        default=None,
        description="Optional config.json key-value overrides",
    )
    register_with_mill: bool = Field(
        default=True,
        description="Whether to register the component with the mill after onboarding",
    )
    port_shifts: list[PortShift] = Field(
        default=[],
        description="Port shift list echoed from preflight; used for collision ticket filing",
    )
    target_disk: str = Field(
        default="",
        description=(
            "Target disk identifier (device path, mount point, or label) "
            "for volume placement. Overrides the value in spec when set. "
            "Empty means use the spec value or config default."
        ),
    )


class OnboardConfirmAcceptedResponse(BaseModel):
    """Returned by POST /onboard/confirm (202) when the job is queued."""

    job_id: str = Field(description="Unique identifier for the queued onboard job")
    name: str = Field(description="Component name")


class OnboardJobStatusResponse(_JobStatusFields):
    """Returned by GET /onboard/jobs/{job_id}."""

    component: str = Field(
        description="Component ID returned by the mill after registration"
    )
    phase: OnboardJobPhase = Field(
        description="Current phase of the onboarding workflow"
    )
    warnings: list[str] = Field(
        default=[],
        description="Non-empty when the mill was unreachable during port-shift ticket filing",
    )


# ---------------------------------------------------------------------------
# Deploy job models (async deploy pattern)
# ---------------------------------------------------------------------------
