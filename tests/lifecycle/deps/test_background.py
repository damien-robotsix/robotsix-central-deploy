"""Direct unit tests for the background registry-check logic.

Covers ``_check_and_update_record`` and ``_registry_check_loop`` from
``robotsix_central_deploy.lifecycle.deps.background``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from robotsix_central_deploy.lifecycle.deps.background import (
    _check_and_update_record,
    _registry_check_loop,
)
from robotsix_central_deploy.lifecycle.models import ComponentInspect, ServiceRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    name: str = "test-svc",
    image: str = "ghcr.io/org/svc:main",
    deployed_image_digest: str = "",
    update_available: bool = False,
    latest_registry_digest: str = "",
    registry_auth_error: bool = False,
) -> ServiceRecord:
    return ServiceRecord(
        name=name,
        image=image,
        deployed_image_digest=deployed_image_digest,
        update_available=update_available,
        latest_registry_digest=latest_registry_digest,
        registry_auth_error=registry_auth_error,
    )


# ===========================================================================
# _check_and_update_record
# ===========================================================================


class TestCheckAndUpdateRecord:
    """Direct unit tests for ``_check_and_update_record``."""

    # -- digest backfill ---------------------------------------------------

    async def test_digest_backfill_when_deployed_digest_absent(self) -> None:
        """When ``deployed_image_digest`` is empty but ``image`` is set,
        ``backend.status()`` is called and its ``running_digest`` is persisted.
        """
        record = _make_record(deployed_image_digest="")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(
                state="RUNNING",
                running_digest="sha256:abc123",
            )
        )

        await _check_and_update_record(record, store, checker, backend)

        assert record.deployed_image_digest == "sha256:abc123"
        store.put.assert_awaited_once_with(record)

    async def test_no_backfill_when_running_digest_empty(self) -> None:
        """When ``backend.status()`` returns an empty ``running_digest``,
        the record is not persisted and the digest stays empty.
        """
        record = _make_record(deployed_image_digest="")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state="STOPPED", running_digest="")
        )

        await _check_and_update_record(record, store, checker, backend)

        assert record.deployed_image_digest == ""
        store.put.assert_not_awaited()

    async def test_no_backfill_when_no_image_set(self) -> None:
        """When ``image`` is empty, the function returns immediately without
        calling the backend or checker.
        """
        record = _make_record(image="", deployed_image_digest="")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock()
        backend = MagicMock()
        backend.status = AsyncMock()

        await _check_and_update_record(record, store, checker, backend)

        backend.status.assert_not_awaited()
        store.put.assert_not_awaited()
        checker.get_latest_digest.assert_not_awaited()

    # -- update_available flip ---------------------------------------------

    async def test_update_available_flipped_when_latest_differs(self) -> None:
        """When the registry digest differs from the deployed digest,
        ``update_available`` is set to ``True`` and the record is persisted.
        """
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:new")
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.update_available is True
        assert record.latest_registry_digest == "sha256:new"
        store.put.assert_awaited_once_with(record)

    async def test_update_available_cleared_when_latest_matches(self) -> None:
        """When the registry digest matches the deployed digest,
        ``update_available`` is set back to ``False`` and persisted.
        """
        record = _make_record(
            deployed_image_digest="sha256:same",
            update_available=True,
            latest_registry_digest="sha256:old-latest",
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:same")
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.update_available is False
        assert record.latest_registry_digest == "sha256:same"
        store.put.assert_awaited_once_with(record)

    # -- persistence only on change ----------------------------------------

    async def test_no_persistence_when_state_unchanged(self) -> None:
        """When both ``update_available`` and ``latest_registry_digest``
        are already correct, ``store.put`` is not called.
        """
        record = _make_record(
            deployed_image_digest="sha256:same",
            update_available=False,
            latest_registry_digest="sha256:same",
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:same")
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        store.put.assert_not_awaited()

    async def test_persistence_when_only_latest_digest_changes(self) -> None:
        """When ``latest_registry_digest`` changes but ``update_available``
        stays the same, the record is still persisted.
        """
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=True,
            latest_registry_digest="sha256:prev-new",
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:different-new")
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.latest_registry_digest == "sha256:different-new"
        store.put.assert_awaited_once_with(record)

    # -- early return when no image or no digest ---------------------------

    async def test_early_return_when_no_image_and_digest_present(self) -> None:
        """When ``image`` is empty but ``deployed_image_digest`` is set,
        checker is never called (early return).
        """
        record = _make_record(image="", deployed_image_digest="sha256:abc")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        checker.get_latest_digest.assert_not_called()
        store.put.assert_not_awaited()

    async def test_early_return_when_no_deployed_digest_after_backfill(self) -> None:
        """When the backfill fails to populate ``deployed_image_digest``,
        the function returns without calling the checker.
        """
        record = _make_record(deployed_image_digest="")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state="RUNNING", running_digest="")
        )

        await _check_and_update_record(record, store, checker, backend)

        checker.get_latest_digest.assert_not_called()

    # -- exception resilience ----------------------------------------------

    async def test_backend_status_failure_is_swallowed(self) -> None:
        """When ``backend.status()`` raises, the function continues and does
        not crash — the digest stays empty.
        """
        record = _make_record(deployed_image_digest="")
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        backend = MagicMock()
        backend.status = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise.
        await _check_and_update_record(record, store, checker, backend)

        assert record.deployed_image_digest == ""
        store.put.assert_not_awaited()

    async def test_checker_get_latest_digest_failure_is_swallowed(self) -> None:
        """When ``checker.get_latest_digest()`` raises, the function does not
        crash and the record is left unchanged.
        """
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
            latest_registry_digest="sha256:old-latest",
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(side_effect=ConnectionError("down"))
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.update_available is False
        assert record.latest_registry_digest == "sha256:old-latest"
        store.put.assert_not_awaited()

    async def test_checker_returns_none_is_noop(self) -> None:
        """When ``checker.get_latest_digest()`` returns ``None``, the record
        is not updated — treat as a miss, not an error.
        """
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value=None)
        checker.was_auth_error = MagicMock(return_value=False)
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.update_available is False
        store.put.assert_not_awaited()

    async def test_store_put_failure_is_swallowed(self) -> None:
        """When ``store.put()`` raises after a state change, the function
        does not crash.
        """
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
        )
        store = MagicMock()
        store.put = AsyncMock(side_effect=OSError("disk full"))
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:new")
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        # Record fields were updated in-memory despite the store failure.
        assert record.update_available is True
        assert record.latest_registry_digest == "sha256:new"

    # -- auth-error propagation -------------------------------------------

    async def test_auth_error_set_when_checker_flags_401(self) -> None:
        """When get_latest_digest returns None and was_auth_error is True,
        registry_auth_error is set and the record is persisted."""
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value=None)
        checker.was_auth_error = MagicMock(return_value=True)
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.registry_auth_error is True
        store.put.assert_awaited_once_with(record)

    async def test_auth_error_cleared_when_fetch_succeeds(self) -> None:
        """When a subsequent check succeeds, registry_auth_error is cleared."""
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
            registry_auth_error=True,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:old")
        checker.was_auth_error = MagicMock(return_value=False)
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.registry_auth_error is False
        store.put.assert_awaited_once_with(record)

    async def test_auth_error_cleared_on_generic_failure(self) -> None:
        """When the checker returns None but was_auth_error is False,
        previously-set auth_error is cleared."""
        record = _make_record(
            deployed_image_digest="sha256:old",
            update_available=False,
            registry_auth_error=True,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value=None)
        checker.was_auth_error = MagicMock(return_value=False)
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.registry_auth_error is False
        store.put.assert_awaited_once_with(record)

    async def test_auth_error_noop_when_already_set(self) -> None:
        """When registry_auth_error is already True and another auth error
        occurs, store.put is NOT called (no state change)."""
        record = _make_record(
            deployed_image_digest="sha256:old",
            registry_auth_error=True,
        )
        store = MagicMock(put=AsyncMock())
        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value=None)
        checker.was_auth_error = MagicMock(return_value=True)
        backend = MagicMock()

        await _check_and_update_record(record, store, checker, backend)

        assert record.registry_auth_error is True
        store.put.assert_not_awaited()


# ===========================================================================
# _registry_check_loop
# ===========================================================================


class TestRegistryCheckLoop:
    """Direct unit tests for ``_registry_check_loop``."""

    async def test_iterates_all_records(self) -> None:
        """The loop processes every record returned by ``store.list_all()``,
        backfilling digests and checking for updates.
        """
        r1 = _make_record(name="svc-a", deployed_image_digest="")
        r2 = _make_record(name="svc-b", deployed_image_digest="sha256:abc")
        store = MagicMock()
        store.list_all = AsyncMock(return_value=[r1, r2])
        store.put = AsyncMock()

        checker = MagicMock()
        checker.get_latest_digest = AsyncMock(return_value="sha256:xyz")
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(
                state="RUNNING", running_digest="sha256:abc123"
            )
        )

        task = asyncio.create_task(
            _registry_check_loop(store, checker, backend, interval_sec=0)
        )
        # Let at least one cycle run.
        await asyncio.sleep(0.02)
        task.cancel()
        # The loop catches CancelledError internally, so awaiting the
        # cancelled task completes without raising.
        await task

        # r1 should have been backfilled.
        assert r1.deployed_image_digest == "sha256:abc123"
        # r2 should have been checked against the registry.
        assert r2.latest_registry_digest == "sha256:xyz"
        assert r2.update_available is True

    async def test_cancellation_is_clean(self) -> None:
        """When the task is cancelled during its sleep, the loop catches
        ``CancelledError`` and exits cleanly (no exception propagates).
        """
        store = MagicMock()
        store.list_all = AsyncMock(return_value=[])
        checker = MagicMock()
        backend = MagicMock()

        task = asyncio.create_task(
            _registry_check_loop(store, checker, backend, interval_sec=60)
        )
        # Give the task a moment to enter its sleep.
        await asyncio.sleep(0.01)
        task.cancel()
        # The loop catches CancelledError — awaiting the cancelled
        # task should complete without raising.
        await task
