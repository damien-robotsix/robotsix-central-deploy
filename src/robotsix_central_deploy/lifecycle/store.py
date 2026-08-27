"""Persistence layer for service records.

Provides an abstract ``ServiceStore`` and two implementations:
* ``InMemoryStore`` — fast, ephemeral (dict + asyncio lock).
* ``FileStore`` — YAML-backed, survives restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from robotsix_central_deploy.lifecycle._yaml_utils import (
    InvalidConfigStructureError,
    read_yaml_file,
)

from .models import ServiceRecord, ServiceState

logger = logging.getLogger(__name__)


class ServiceStore(ABC):
    """Abstract persistence for managed-service records."""

    @abstractmethod
    async def get(self, name: str) -> ServiceRecord | None:
        """Return the record for *name*, or *None* if not found."""
        ...

    @abstractmethod
    async def put(self, record: ServiceRecord) -> None:
        """Store *record*, keyed by its name."""
        ...

    @abstractmethod
    async def delete(self, name: str) -> bool:
        """Remove the record for *name*. Returns True if it existed."""
        ...

    @abstractmethod
    async def list_all(self) -> list[ServiceRecord]:
        """Return all stored records as a list."""
        ...

    @abstractmethod
    async def count(self) -> int:
        """Return the number of stored records."""
        ...


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class InMemoryStore(ServiceStore):
    """Ephemeral dict-backed store with an asyncio lock for safety."""

    def __init__(self) -> None:
        self._data: dict[str, ServiceRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> ServiceRecord | None:
        """Return the record for *name*, or *None* if not found."""
        async with self._lock:
            return self._data.get(name)

    async def put(self, record: ServiceRecord) -> None:
        """Store *record*, keyed by its name."""
        record.updated_at = time.time()
        async with self._lock:
            self._data[record.name] = record

    async def delete(self, name: str) -> bool:
        """Remove the record for *name*. Returns True if it existed."""
        async with self._lock:
            return self._data.pop(name, None) is not None

    async def list_all(self) -> list[ServiceRecord]:
        """Return all stored records as a list."""
        async with self._lock:
            return list(self._data.values())

    async def count(self) -> int:
        """Return the number of stored records."""
        async with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# File store
# ---------------------------------------------------------------------------


class FileStore(ServiceStore):
    """YAML-file persistence.  Not safe for concurrent processes — single-writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, ServiceRecord]:
        """Load and parse the YAML state file; returns empty dict on missing or invalid file."""
        if not self._path.exists():
            return {}
        try:
            raw = read_yaml_file(self._path)
        except InvalidConfigStructureError:
            # A truncated/empty state file (e.g. from an interrupted write on a
            # prior version) parses to None at the top level. Crashing here
            # takes the whole gateway down in a restart loop; instead self-heal
            # by treating it as empty so the registry re-seeds the records.
            logger.warning(
                "State file %s is empty/invalid (top-level not a mapping); "
                "treating as empty and re-seeding from the component registry.",
                self._path,
            )
            return {}
        records: dict[str, ServiceRecord] = {}
        for name, d in raw.items():
            d = d or {}
            records[name] = ServiceRecord(
                name=name,
                image=d.get("image", ""),
                state=ServiceState(d.get("state", "unknown")),
                last_error=d.get("last_error", ""),
                updated_at=d.get("updated_at", 0.0),
                container_name=d.get("container_name", ""),
                image_revision=d.get("image_revision", ""),
                health=d.get("health", ""),
                deployed_image_digest=d.get("deployed_image_digest", ""),
                previous_image_digest=d.get("previous_image_digest", ""),
                update_available=d.get("update_available", False),
                latest_registry_digest=d.get("latest_registry_digest", ""),
                registry_auth_error=d.get("registry_auth_error", False),
                component_id=d.get("component_id", ""),
                repo_id=d.get("repo_id", ""),
            )
        return records

    async def _save(self, records: dict[str, ServiceRecord]) -> None:
        """Atomically write *records* to the YAML state file via temp-file + os.replace."""
        raw: dict[str, dict[str, object]] = {}
        for name, r in records.items():
            raw[name] = {
                "image": r.image,
                "state": r.state.value,
                "last_error": r.last_error,
                "updated_at": r.updated_at,
                "container_name": r.container_name,
                "image_revision": r.image_revision,
                "health": r.health,
                "deployed_image_digest": r.deployed_image_digest,
                "previous_image_digest": r.previous_image_digest,
                "update_available": r.update_available,
                "latest_registry_digest": r.latest_registry_digest,
                "registry_auth_error": r.registry_auth_error,
                "component_id": r.component_id,
                "repo_id": r.repo_id,
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(raw, default_flow_style=False)
        # Atomic write: render to a temp file in the same directory, fsync it,
        # then os.replace onto the target. os.replace is atomic on POSIX (same
        # filesystem), so a crash/kill/disk-pressure mid-write can never leave a
        # truncated state file behind — the old file survives intact until the
        # complete new one swaps in. (Root cause of the 07-24 gateway outage.)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=f"{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        # Best-effort: fsync the directory so the rename itself is durable.
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    async def get(self, name: str) -> ServiceRecord | None:
        """Return the record for *name*, or *None* if not found."""
        records = await self._load()
        return records.get(name)

    async def put(self, record: ServiceRecord) -> None:
        """Store *record* atomically under lock."""
        record.updated_at = time.time()
        async with self._lock:
            records = await self._load()
            records[record.name] = record
            await self._save(records)

    async def delete(self, name: str) -> bool:
        """Remove *name* atomically under lock. Returns True if it existed."""
        async with self._lock:
            records = await self._load()
            if name not in records:
                return False
            del records[name]
            await self._save(records)
            return True

    async def list_all(self) -> list[ServiceRecord]:
        """Return all stored records as a list."""
        records = await self._load()
        return list(records.values())

    async def count(self) -> int:
        """Return the number of stored records."""
        records = await self._load()
        return len(records)
