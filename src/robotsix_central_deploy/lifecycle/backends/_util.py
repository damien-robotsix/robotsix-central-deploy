"""Shared backend utilities."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable
from typing import Any

from ..models import ServiceState

# Image refs (ids / digests) belonging to deploys currently in flight.
# A digest-pulled image carries no tag, so Docker reports it as dangling
# until a container references it; a prune running concurrently (another
# deploy's auto-prune, the caretaker sweep, /disk/reclaim) would delete it
# between pull and container create, failing the deploy with "No such
# image". Refcounted because several deploys may target the same image.
_inflight_lock = threading.Lock()
_inflight_image_refs: Counter[str] = Counter()


def register_inflight_image_refs(refs: Iterable[str]) -> None:
    """Protect *refs* from image pruning until released."""
    with _inflight_lock:
        for ref in refs:
            if ref:
                _inflight_image_refs[ref] += 1


def release_inflight_image_refs(refs: Iterable[str]) -> None:
    """Drop the in-flight protection acquired by ``register_inflight_image_refs``."""
    with _inflight_lock:
        for ref in refs:
            if ref and _inflight_image_refs[ref] > 0:
                _inflight_image_refs[ref] -= 1
                if not _inflight_image_refs[ref]:
                    del _inflight_image_refs[ref]


def inflight_image_refs() -> set[str]:
    """Snapshot of refs currently protected by in-flight deploys."""
    with _inflight_lock:
        return set(_inflight_image_refs)


def docker_status_to_service_state(status: str) -> ServiceState:
    """Map a Docker container status string to a ``ServiceState`` enum value."""
    mapping: dict[str, ServiceState] = {
        "running": ServiceState.RUNNING,
        "paused": ServiceState.RUNNING,
        "restarting": ServiceState.RESTARTING,
        "created": ServiceState.STOPPED,
        "exited": ServiceState.STOPPED,
        "dead": ServiceState.FAILED,
        "removing": ServiceState.STOPPING,
    }
    return mapping.get(status.lower(), ServiceState.UNKNOWN)


async def collect_protected_image_refs(store: Any) -> set[str]:
    """Image ids/digests that must survive an image prune.

    Every record's deployed and previous digests are rollback targets;
    ``rollback`` recreates containers from a local image id, which Docker
    cannot re-pull, so pruning them would break rollback. Images pulled by
    deploys still in flight are protected too — they are untagged (hence
    dangling) until their container exists.
    """
    protected: set[str] = set()
    for record in await store.list_all():
        for ref in (
            record.deployed_image_digest,
            record.previous_image_digest,
            record.image_revision,
        ):
            if ref:
                protected.add(ref)
    return protected | inflight_image_refs()
