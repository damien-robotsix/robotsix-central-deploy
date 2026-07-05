"""Unit tests for the per-component deploy lock module."""

from __future__ import annotations

import asyncio

import pytest

from robotsix_central_deploy import deploy_lock


@pytest.fixture(autouse=True)
def _reset_deploy_locks():
    """Reset the module-level lock dict before each test so tests are isolated."""
    deploy_lock._deploy_locks.clear()


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
