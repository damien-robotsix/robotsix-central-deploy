"""Deploy job request / response schemas (async deploy pattern)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from robotsix_central_deploy.lifecycle.models import DeployJobPhase
from robotsix_central_deploy.lifecycle.schemas._base import _JobStatusFields


class DeployAcceptedResponse(BaseModel):
    """Returned by POST /services/{name}/deploy (202) when the job is queued."""

    job_id: str = Field(description="Unique identifier for the queued deploy job")
    name: str = Field(description="Component name")


class DeployJobStatusResponse(_JobStatusFields):
    """Returned by GET /services/deploy-jobs/{job_id}."""

    component: str = Field(description="Component ID")
    phase: DeployJobPhase = Field(description="Current phase of the deploy workflow")
    warnings: list[str] = Field(
        default=[],
        description="Warnings encountered during deploy (e.g. pre-pull failures)",
    )


# ---------------------------------------------------------------------------
# Env endpoint models
# ---------------------------------------------------------------------------
