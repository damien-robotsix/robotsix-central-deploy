"""Per-component deploy lock that serialises concurrent deploys.

Shared by the API endpoint and the caretaker so operator-initiated and
auto-update deploys of the same component don't race into Docker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

#: Per-component asyncio locks that serialise deploys so concurrent
#: operator and caretaker deploys of the same component don't race.
_deploy_locks: dict[str, asyncio.Lock] = {}

#: Per-component metadata about the current lock holder so callers
#: that hit a locked component can surface who holds it and since when.
_lock_info: dict[str, dict[str, Any]] = {}


async def try_acquire_deploy_lock(name: str, source: str = "manual") -> bool:
    """Attempt to acquire the per-component deploy lock for *name*.

    *source* identifies who initiated the deploy (``"manual"`` for
    operator-initiated, ``"caretaker"`` for auto-update).

    Returns ``True`` when the lock was acquired (caller proceeds with
    deploy), ``False`` when a deploy is already in progress for the
    component.
    """
    if name not in _deploy_locks:
        _deploy_locks[name] = asyncio.Lock()
    lock = _deploy_locks[name]
    # lock.locked() → lock.acquire() is safe in asyncio (no await between,
    # so no other task can interleave).
    if lock.locked():
        return False
    await lock.acquire()
    _lock_info[name] = {"source": source, "started_at": time.time(), "job_id": ""}
    return True


def set_deploy_lock_job_id(name: str, job_id: str) -> None:
    """Record the *job_id* on the lock metadata for *name*.

    Called after the deploy job is created so that concurrent callers can
    link to the active job.
    """
    info = _lock_info.get(name)
    if info is not None:
        info["job_id"] = job_id


def get_deploy_lock_info(name: str) -> dict[str, Any] | None:
    """Return metadata about who holds the deploy lock for *name*.

    Returns ``None`` when the lock is not currently held.
    """
    lock = _deploy_locks.get(name)
    if lock is not None and lock.locked():
        return _lock_info.get(name)
    return None


def release_deploy_lock(name: str) -> None:
    """Release the per-component deploy lock for *name*."""
    lock = _deploy_locks.get(name)
    if lock is not None and lock.locked():
        lock.release()
        # If nobody is waiting, drop the entry to avoid dict growth.
        if not lock.locked():
            _deploy_locks.pop(name, None)
            _lock_info.pop(name, None)
