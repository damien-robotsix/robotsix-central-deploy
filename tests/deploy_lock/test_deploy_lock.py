"""Unit tests for the per-component deploy lock module."""

from __future__ import annotations

import asyncio

import pytest

from robotsix_central_deploy import deploy_lock


@pytest.fixture(autouse=True)
def _reset_deploy_locks():
    """Reset the module-level lock dict before each test so tests are isolated."""
    deploy_lock._deploy_locks.clear()
    deploy_lock._lock_info.clear()


class TestTryAcquireDeployLock:
    """Tests for ``try_acquire_deploy_lock``."""

    async def test_acquire_on_first_call_returns_true(self):
        """The first call for a component returns True and holds the lock."""
        result = await deploy_lock.try_acquire_deploy_lock("svc")
        assert result is True

        # The lock should now be held.
        assert "svc" in deploy_lock._deploy_locks
        assert deploy_lock._deploy_locks["svc"].locked()

    async def test_concurrent_call_returns_false(self):
        """A second concurrent call for the same component returns False."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        result = await deploy_lock.try_acquire_deploy_lock("svc")
        assert result is False

    async def test_concurrent_between_tasks(self):
        """A lock held by one task cannot be acquired by another task."""

        async def acquire_in_task():
            return await deploy_lock.try_acquire_deploy_lock("svc")

        # Acquire the lock in the main task.
        await deploy_lock.try_acquire_deploy_lock("svc")

        # A concurrent task attempting the same component must get False.
        result = await asyncio.create_task(acquire_in_task())
        assert result is False


class TestReleaseDeployLock:
    """Tests for ``release_deploy_lock``."""

    async def test_release_unlocks(self):
        """Releasing a held lock makes it available again (entry cleaned up)."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        assert deploy_lock._deploy_locks["svc"].locked()

        deploy_lock.release_deploy_lock("svc")

        # release_deploy_lock pops the entry when nobody is waiting.
        assert "svc" not in deploy_lock._deploy_locks

    async def test_release_cleans_up_dict_entry(self):
        """Releasing a lock with no waiters removes the dict entry."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")
        assert "svc" not in deploy_lock._deploy_locks

    async def test_release_nonexistent_is_noop(self):
        """Releasing a lock that was never acquired does not raise."""
        deploy_lock.release_deploy_lock("svc")  # no error

    async def test_release_on_not_held_is_noop(self):
        """Releasing an already-released lock does not raise."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")  # no error


class TestMultipleComponents:
    """Tests for independent locks per component."""

    async def test_independent_locks(self):
        """Locking one component does not block another."""
        await deploy_lock.try_acquire_deploy_lock("svc-a")
        result = await deploy_lock.try_acquire_deploy_lock("svc-b")
        assert result is True

    async def test_release_one_does_not_affect_other(self):
        """Releasing one component's lock does not touch another's."""
        await deploy_lock.try_acquire_deploy_lock("svc-a")
        await deploy_lock.try_acquire_deploy_lock("svc-b")

        deploy_lock.release_deploy_lock("svc-a")
        assert "svc-a" not in deploy_lock._deploy_locks
        assert "svc-b" in deploy_lock._deploy_locks
        assert deploy_lock._deploy_locks["svc-b"].locked()


class TestReacquireAfterRelease:
    """Tests for re-acquiring a lock after releasing it."""

    async def test_reacquire_after_release(self):
        """After releasing, the same component can acquire the lock again."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")

        result = await deploy_lock.try_acquire_deploy_lock("svc")
        assert result is True
        assert deploy_lock._deploy_locks["svc"].locked()


class TestDeployLockInfo:
    """Tests for lock metadata storage and retrieval."""

    async def test_stores_source_and_started_at_on_acquire(self):
        """Acquiring a lock records the source and a started_at timestamp."""
        await deploy_lock.try_acquire_deploy_lock("svc", source="caretaker")
        info = deploy_lock.get_deploy_lock_info("svc")
        assert info is not None
        assert info["source"] == "caretaker"
        assert isinstance(info["started_at"], float)
        assert info["job_id"] == ""

    async def test_default_source_is_manual(self):
        """When source is not passed, it defaults to 'manual'."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        info = deploy_lock.get_deploy_lock_info("svc")
        assert info["source"] == "manual"

    async def test_get_info_returns_none_when_not_locked(self):
        """get_deploy_lock_info returns None when the lock is not held."""
        assert deploy_lock.get_deploy_lock_info("svc") is None

    async def test_get_info_returns_none_after_release(self):
        """After releasing, get_deploy_lock_info returns None."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")
        assert deploy_lock.get_deploy_lock_info("svc") is None

    async def test_set_job_id_updates_existing_info(self):
        """set_deploy_lock_job_id patches the job_id on the lock metadata."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.set_deploy_lock_job_id("svc", "job-123")
        info = deploy_lock.get_deploy_lock_info("svc")
        assert info["job_id"] == "job-123"

    async def test_set_job_id_noop_when_not_locked(self):
        """set_deploy_lock_job_id is a no-op when no lock info exists."""
        deploy_lock.set_deploy_lock_job_id("svc", "job-123")  # no error

    async def test_info_cleared_on_release(self):
        """Releasing the lock also removes the lock info entry."""
        await deploy_lock.try_acquire_deploy_lock("svc")
        deploy_lock.release_deploy_lock("svc")
        assert "svc" not in deploy_lock._lock_info
