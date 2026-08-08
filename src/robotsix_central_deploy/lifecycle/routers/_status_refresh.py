"""Shared backend-status refresh for service status endpoints.

Both ``GET /services/{name}`` and ``GET /chat/services/{name}/status``
must refresh a :class:`~..models.ServiceRecord` from the execution
backend and the image registry before answering.  The sequence is
identical for both routes and lives here so a change to the refresh
logic (a new inspect field, a new registry-check behavior) is applied
in exactly one place.
"""

from __future__ import annotations

import logging

from ...registry_check import RegistryChecker
from ..backends import ExecutionBackend
from ..models import ComponentInspect, ServiceRecord
from ..store import ServiceStore

logger = logging.getLogger(__name__)


async def refresh_record_status(
    record: ServiceRecord,
    backend: ExecutionBackend,
    store: ServiceStore,
    checker: RegistryChecker,
) -> ComponentInspect:
    """Refresh *record* from the backend and registry, persisting changes.

    Steps (all best-effort):

    1. Inspect the running container and persist ``state`` /
       ``image_revision`` / ``health`` when they drifted.
    2. Persist ``running_digest`` into ``deployed_image_digest``.
    3. Compare the latest registry digest against the deployed digest
       and persist ``update_available`` / ``latest_registry_digest``.
       Registry failures degrade gracefully — the last known
       ``update_available`` is kept and the failure is logged at DEBUG.

    Returns the fresh :class:`ComponentInspect` so callers can derive
    additional data (e.g. overall health) from the same inspection.
    """
    inspect = await backend.status(record)
    changed = (
        inspect.state != record.state
        or inspect.image_revision != record.image_revision
        or inspect.health != record.health
    )
    if changed:
        record.state = inspect.state
        record.image_revision = inspect.image_revision
        record.health = inspect.health
        await store.put(record)

    if (
        inspect.running_digest
        and inspect.running_digest != record.deployed_image_digest
    ):
        record.deployed_image_digest = inspect.running_digest
        await store.put(record)

    if record.image and record.deployed_image_digest:
        try:
            latest = await checker.get_latest_digest(record.image)
            if latest is not None:
                new_ua = latest != record.deployed_image_digest
                if (
                    record.update_available != new_ua
                    or record.latest_registry_digest != latest
                ):
                    record.update_available = new_ua
                    record.latest_registry_digest = latest
                    await store.put(record)
        except Exception:
            logger.debug(
                "status refresh: registry check failed for %s",
                repr(record.name),
                exc_info=True,
            )

    return inspect
