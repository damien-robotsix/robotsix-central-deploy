"""Per-component deploy lock that serialises concurrent deploys.

Shared by the API endpoint and the caretaker so operator-initiated and
auto-update deploys of the same component don't race into Docker.
"""

from __future__ import annotations

import asyncio

#: Per-component asyncio locks that serialise deploys so concurrent
#: operator and caretaker deploys of the same component don't race.
_deploy_locks: dict[str, asyncio.Lock] = {}


async def try_acquire_deploy_lock(name: str) -> bool:
    """Attempt to acquire the per-component deploy lock for *name*.

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
    return True


def release_deploy_lock(name: str) -> None:
    """Release the per-component deploy lock for *name*."""
    lock = _deploy_locks.get(name)
    if lock is not None and lock.locked():
        lock.release()
        # If nobody is waiting, drop the entry to avoid dict growth.
        if not lock.locked():
            _deploy_locks.pop(name, None)
