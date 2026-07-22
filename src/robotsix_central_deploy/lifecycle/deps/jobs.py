"""In-memory job registry for onboard and deploy background jobs."""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from ..models import DeployJobPhase, OnboardJobPhase


class Job:
    """In-memory record of one background job (onboard confirm or deploy)."""

    __slots__ = (
        "job_id",
        "component",
        "phase",
        "error",
        "logs",
        "name",
        "image",
        "state",
        "warnings",
    )

    # Class-level annotations so mypy sees the __slots__ attributes.
    job_id: str
    component: str
    phase: Enum
    error: str | None
    logs: str | None
    name: str | None
    image: str | None
    state: str | None
    warnings: list[str]

    variant: ClassVar[type[Enum]]

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id = job_id
        self.component = component
        # phase is set by the subclass __init__ to the correct default.
        self.error = None
        self.logs = None
        self.name = None
        self.image = None
        self.state = None
        self.warnings = []


class OnboardJob(Job):
    """In-memory record of one onboard confirm background deploy job."""

    variant = OnboardJobPhase

    def __init__(self, job_id: str, component: str) -> None:
        super().__init__(job_id, component)
        self.phase: OnboardJobPhase = OnboardJobPhase.WRITING_CONFIG


class DeployJob(Job):
    """In-memory record of one background deploy job (API-initiated)."""

    variant = DeployJobPhase

    def __init__(self, job_id: str, component: str) -> None:
        super().__init__(job_id, component)
        self.phase: DeployJobPhase = DeployJobPhase.DEPLOYING


class JobRegistry:
    """Thread-safe-ish in-memory registry for onboard and deploy background jobs.

    The app is single-process asyncio; no lock is needed for simple
    dict access under the same event loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter: int = 0

    # -- creation -----------------------------------------------------------

    def create(self, component: str) -> str:
        """Create a new onboard job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._jobs[job_id] = OnboardJob(job_id=job_id, component=component)
        return job_id

    def create_deploy(self, component: str) -> str:
        """Create a new deploy job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._jobs[job_id] = DeployJob(job_id=job_id, component=component)
        return job_id

    # -- unified accessors --------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        """Return a job by id, or None."""
        return self._jobs.get(job_id)

    def update_phase(self, job_id: str, phase: Enum) -> None:
        """Update the phase of a job."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_failed(self, job_id: str, error: str, logs: str | None = None) -> None:
        """Mark a job as failed with an error string and optional logs."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = getattr(type(job).variant, "FAILED")
            job.error = error
            if logs is not None:
                job.logs = logs

    def mark_done(
        self,
        job_id: str,
        name: str,
        image: str,
        state: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark a job as done with terminal fields."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = getattr(type(job).variant, "DONE")
            job.name = name
            job.image = image
            job.state = state
            job.warnings = warnings or []

    def has_active_job_for(self, component: str) -> bool:
        """Return True when an onboard job for *component* is still in flight."""
        return any(
            isinstance(j, OnboardJob)
            and j.component == component
            and j.phase not in (OnboardJobPhase.DONE, OnboardJobPhase.FAILED)
            for j in self._jobs.values()
        )

    def active_deploy_job_id_for(self, component: str) -> str | None:
        """Return the job_id of an active deploy job for *component*, or None."""
        for job in self._jobs.values():
            if (
                isinstance(job, DeployJob)
                and job.component == component
                and job.phase not in (DeployJobPhase.DONE, DeployJobPhase.FAILED)
            ):
                return job.job_id
        return None
