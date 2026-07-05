"""Shared backend utilities."""

from __future__ import annotations

from typing import Any

from ..models import ServiceState


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
    cannot re-pull, so pruning them would break rollback.
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
    return protected
