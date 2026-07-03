"""Shared backend utilities."""

from __future__ import annotations

from typing import Any


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
