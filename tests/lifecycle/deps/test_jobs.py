"""Direct tests for the Job / JobRegistry classes in lifecycle.deps.jobs."""

from __future__ import annotations

import pytest

from robotsix_central_deploy.lifecycle.deps.jobs import (
    DeployJob,
    Job,
    JobRegistry,
    OnboardJob,
)
from robotsix_central_deploy.lifecycle.models import DeployJobPhase, OnboardJobPhase


class TestJob:
    """Tests for the base ``Job`` class."""

    def test_job_attributes_default(self) -> None:
        """A fresh Job has correct defaults (phase set by subclass)."""
        job = Job("j1", "mail")
        assert job.job_id == "j1"
        assert job.component == "mail"
        assert job.error is None
        assert job.name is None
        assert job.image is None
        assert job.state is None
        assert job.warnings == []

    def test_job_slots_are_writable(self) -> None:
        """Job __slots__ attributes can be written after construction."""
        job = Job("j2", "web")
        job.error = "something broke"
        job.name = "web-service"
        job.image = "ghcr.io/org/web:latest"
        job.state = "RUNNING"
        job.warnings = ["low disk"]
        assert job.error == "something broke"
        assert job.name == "web-service"
        assert job.image == "ghcr.io/org/web:latest"
        assert job.state == "RUNNING"
        assert job.warnings == ["low disk"]


class TestOnboardJob:
    """Tests for the ``OnboardJob`` subclass."""

    def test_onboard_job_phase_default(self) -> None:
        """An OnboardJob starts in WRITING_CONFIG phase."""
        job = OnboardJob("oj1", "mail")
        assert job.phase == OnboardJobPhase.WRITING_CONFIG

    def test_onboard_job_variant(self) -> None:
        """OnboardJob.variant is OnboardJobPhase."""
        assert OnboardJob.variant is OnboardJobPhase

    def test_onboard_job_is_job(self) -> None:
        """OnboardJob is an instance of Job."""
        job = OnboardJob("oj2", "web")
        assert isinstance(job, Job)


class TestDeployJob:
    """Tests for the ``DeployJob`` subclass."""

    def test_deploy_job_phase_default(self) -> None:
        """A DeployJob starts in DEPLOYING phase."""
        job = DeployJob("dj1", "mail")
        assert job.phase == DeployJobPhase.DEPLOYING

    def test_deploy_job_variant(self) -> None:
        """DeployJob.variant is DeployJobPhase."""
        assert DeployJob.variant is DeployJobPhase

    def test_deploy_job_is_job(self) -> None:
        """DeployJob is an instance of Job."""
        job = DeployJob("dj2", "web")
        assert isinstance(job, Job)


class TestJobRegistry:
    """Tests for the ``JobRegistry`` in-memory job tracker."""

    @pytest.fixture
    def registry(self) -> JobRegistry:
        return JobRegistry()

    # -- creation -------------------------------------------------------

    def test_create_returns_id(self, registry: JobRegistry) -> None:
        """create() returns a string id and stores an OnboardJob."""
        jid = registry.create("mail")
        assert isinstance(jid, str)
        assert jid.startswith("mail-")
        job = registry.get(jid)
        assert isinstance(job, OnboardJob)
        assert job.component == "mail"
        assert job.phase == OnboardJobPhase.WRITING_CONFIG

    def test_create_deploy_returns_id(self, registry: JobRegistry) -> None:
        """create_deploy() returns a string id and stores a DeployJob."""
        jid = registry.create_deploy("mail")
        assert isinstance(jid, str)
        assert jid.startswith("mail-")
        job = registry.get(jid)
        assert isinstance(job, DeployJob)
        assert job.component == "mail"
        assert job.phase == DeployJobPhase.DEPLOYING

    def test_create_increments_counter(self, registry: JobRegistry) -> None:
        """Each create call produces a unique, incrementing id suffix."""
        jid1 = registry.create("mail")
        jid2 = registry.create("mail")
        assert jid1 != jid2
        assert jid1.endswith("-1")
        assert jid2.endswith("-2")

    # -- get ------------------------------------------------------------

    def test_get_unknown_returns_none(self, registry: JobRegistry) -> None:
        """get() on an unknown job id returns None."""
        assert registry.get("nope-99") is None

    # -- update_phase ---------------------------------------------------

    def test_update_phase_changes_phase(self, registry: JobRegistry) -> None:
        """update_phase() transitions the job's phase."""
        jid = registry.create("mail")
        registry.update_phase(jid, OnboardJobPhase.DEPLOYING_PRIMARY)
        job = registry.get(jid)
        assert job.phase == OnboardJobPhase.DEPLOYING_PRIMARY

    def test_update_phase_unknown_id_noop(self, registry: JobRegistry) -> None:
        """update_phase() on an unknown job id does not raise."""
        registry.update_phase("nope-99", OnboardJobPhase.DONE)

    # -- mark_failed ----------------------------------------------------

    def test_mark_failed_sets_error_and_phase(self, registry: JobRegistry) -> None:
        """mark_failed() transitions to FAILED and records the error."""
        jid = registry.create("mail")
        registry.mark_failed(jid, "network timeout")
        job = registry.get(jid)
        assert job.phase == OnboardJobPhase.FAILED
        assert job.error == "network timeout"

    def test_mark_failed_unknown_id_noop(self, registry: JobRegistry) -> None:
        """mark_failed() on an unknown job id does not raise."""
        registry.mark_failed("nope-99", "error")

    # -- mark_done ------------------------------------------------------

    def test_mark_done_sets_terminal_fields(self, registry: JobRegistry) -> None:
        """mark_done() transitions to DONE and populates name/image/state/warnings."""
        jid = registry.create("mail")
        registry.mark_done(
            jid,
            name="mail-service",
            image="ghcr.io/org/mail:main",
            state="RUNNING",
            warnings=["disk low"],
        )
        job = registry.get(jid)
        assert job.phase == OnboardJobPhase.DONE
        assert job.name == "mail-service"
        assert job.image == "ghcr.io/org/mail:main"
        assert job.state == "RUNNING"
        assert job.warnings == ["disk low"]

    def test_mark_done_default_warnings(self, registry: JobRegistry) -> None:
        """mark_done() defaults warnings to empty list when not provided."""
        jid = registry.create("mail")
        registry.mark_done(jid, name="mail", image="img", state="RUNNING")
        job = registry.get(jid)
        assert job.warnings == []

    def test_mark_done_unknown_id_noop(self, registry: JobRegistry) -> None:
        """mark_done() on an unknown job id does not raise."""
        registry.mark_done("nope-99", name="x", image="y", state="z")

    # -- has_active_job_for ---------------------------------------------

    def test_has_active_job_for_true(self, registry: JobRegistry) -> None:
        """has_active_job_for() returns True when an active OnboardJob exists."""
        registry.create("mail")
        assert registry.has_active_job_for("mail") is True

    def test_has_active_job_for_false_after_done(self, registry: JobRegistry) -> None:
        """has_active_job_for() returns False after the job is marked DONE."""
        jid = registry.create("mail")
        registry.mark_done(jid, name="mail", image="img", state="RUNNING")
        assert registry.has_active_job_for("mail") is False

    def test_has_active_job_for_false_after_failed(self, registry: JobRegistry) -> None:
        """has_active_job_for() returns False after the job is marked FAILED."""
        jid = registry.create("mail")
        registry.mark_failed(jid, "boom")
        assert registry.has_active_job_for("mail") is False

    def test_has_active_job_for_different_component(
        self, registry: JobRegistry
    ) -> None:
        """has_active_job_for() only matches the given component."""
        registry.create("mail")
        assert registry.has_active_job_for("web") is False

    def test_has_active_job_for_ignores_deploy_jobs(
        self, registry: JobRegistry
    ) -> None:
        """has_active_job_for() ignores DeployJob instances."""
        registry.create_deploy("mail")
        assert registry.has_active_job_for("mail") is False

    # -- active_deploy_job_id_for ---------------------------------------

    def test_active_deploy_job_id_for_returns_id(self, registry: JobRegistry) -> None:
        """active_deploy_job_id_for() returns the job id of an active DeployJob."""
        jid = registry.create_deploy("mail")
        result = registry.active_deploy_job_id_for("mail")
        assert result == jid

    def test_active_deploy_job_id_for_returns_none_after_done(
        self, registry: JobRegistry
    ) -> None:
        """active_deploy_job_id_for() returns None when the DeployJob is DONE."""
        jid = registry.create_deploy("mail")
        registry.mark_done(jid, name="mail", image="img", state="RUNNING")
        assert registry.active_deploy_job_id_for("mail") is None

    def test_active_deploy_job_id_for_returns_none_after_failed(
        self, registry: JobRegistry
    ) -> None:
        """active_deploy_job_id_for() returns None when the DeployJob is FAILED."""
        jid = registry.create_deploy("mail")
        registry.mark_failed(jid, "boom")
        assert registry.active_deploy_job_id_for("mail") is None

    def test_active_deploy_job_id_for_different_component(
        self, registry: JobRegistry
    ) -> None:
        """active_deploy_job_id_for() only matches the given component."""
        registry.create_deploy("mail")
        assert registry.active_deploy_job_id_for("web") is None

    def test_active_deploy_job_id_for_ignores_onboard_jobs(
        self, registry: JobRegistry
    ) -> None:
        """active_deploy_job_id_for() ignores OnboardJob instances."""
        registry.create("mail")
        assert registry.active_deploy_job_id_for("mail") is None
