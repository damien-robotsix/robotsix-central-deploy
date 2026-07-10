"""In-memory job registry for onboard and deploy background jobs."""

from __future__ import annotations

from ..models import DeployJobPhase, OnboardJobPhase


class OnboardJob:
    """In-memory record of one onboard confirm background deploy job."""

    __slots__ = (
        "job_id",
        "component",
        "phase",
        "error",
        "name",
        "image",
        "state",
        "warnings",
    )

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id: str = job_id
        self.component: str = component
        self.phase: OnboardJobPhase = OnboardJobPhase.WRITING_CONFIG
        self.error: str | None = None
        self.name: str | None = None
        self.image: str | None = None
        self.state: str | None = None
        self.warnings: list[str] = []


class DeployJob:
    """In-memory record of one background deploy job (API-initiated)."""

    __slots__ = (
        "job_id",
        "component",
        "phase",
        "error",
        "name",
        "image",
        "state",
        "warnings",
    )

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id: str = job_id
        self.component: str = component
        self.phase: DeployJobPhase = DeployJobPhase.DEPLOYING
        self.error: str | None = None
        self.name: str | None = None
        self.image: str | None = None
        self.state: str | None = None
        self.warnings: list[str] = []


class JobRegistry:
    """Thread-safe-ish in-memory registry for onboard and deploy background jobs.

    The app is single-process asyncio; no lock is needed for simple
    dict access under the same event loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, OnboardJob] = {}
        self._deploy_jobs: dict[str, DeployJob] = {}
        self._counter: int = 0

    # -- onboard jobs -------------------------------------------------------

    def create(self, component: str) -> str:
        """Create a new onboard job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._jobs[job_id] = OnboardJob(job_id=job_id, component=component)
        return job_id

    def get(self, job_id: str) -> OnboardJob | None:
        """Return an onboard job by id, or None."""
        return self._jobs.get(job_id)

    def update_phase(self, job_id: str, phase: OnboardJobPhase) -> None:
        """Update the phase of an onboard job."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark an onboard job as failed with an error string."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = OnboardJobPhase.FAILED
            job.error = error

    def mark_done(
        self,
        job_id: str,
        name: str,
        image: str,
        state: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark an onboard job as done with terminal fields."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = OnboardJobPhase.DONE
            job.name = name
            job.image = image
            job.state = state
            job.warnings = warnings or []

    def has_active_job_for(self, component: str) -> bool:
        """Return True when an onboard job for *component* is still in flight."""
        return any(
            j.component == component
            and j.phase not in (OnboardJobPhase.DONE, OnboardJobPhase.FAILED)
            for j in self._jobs.values()
        )

    # -- deploy jobs --------------------------------------------------------

    def create_deploy(self, component: str) -> str:
        """Create a new deploy job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._deploy_jobs[job_id] = DeployJob(job_id=job_id, component=component)
        return job_id

    def get_deploy(self, job_id: str) -> DeployJob | None:
        """Return a deploy job by id, or None."""
        return self._deploy_jobs.get(job_id)

    def update_deploy_phase(self, job_id: str, phase: DeployJobPhase) -> None:
        """Update the phase of a deploy job."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_deploy_failed(self, job_id: str, error: str) -> None:
        """Mark a deploy job as failed with an error string."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = DeployJobPhase.FAILED
            job.error = error

    def mark_deploy_done(
        self,
        job_id: str,
        name: str,
        image: str,
        state: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark a deploy job as done with terminal fields."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = DeployJobPhase.DONE
            job.name = name
            job.image = image
            job.state = state
            job.warnings = warnings or []

    def active_deploy_job_id_for(self, component: str) -> str | None:
        """Return the job_id of an active deploy job for *component*, or None."""
        for job in self._deploy_jobs.values():
            if job.component == component and job.phase not in (
                DeployJobPhase.DONE,
                DeployJobPhase.FAILED,
            ):
                return job.job_id
        return None
