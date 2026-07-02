"""Persistence layer for service records.

Provides an abstract ``ServiceStore`` and two implementations:
* ``InMemoryStore`` — fast, ephemeral (dict + asyncio lock).
* ``FileStore`` — YAML-backed, survives restarts.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from robotsix_yaml_config import read_yaml_file

from .models import ServiceRecord, ServiceState


class ServiceStore(ABC):
    """Abstract persistence for managed-service records."""

    @abstractmethod
    async def get(self, name: str) -> ServiceRecord | None: ...

    @abstractmethod
    async def put(self, record: ServiceRecord) -> None: ...

    @abstractmethod
    async def delete(self, name: str) -> bool: ...

    @abstractmethod
    async def list_all(self) -> list[ServiceRecord]: ...

    @abstractmethod
    async def count(self) -> int: ...


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class InMemoryStore(ServiceStore):
    """Ephemeral dict-backed store with an asyncio lock for safety."""

    def __init__(self) -> None:
        self._data: dict[str, ServiceRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> ServiceRecord | None:
        async with self._lock:
            return self._data.get(name)

    async def put(self, record: ServiceRecord) -> None:
        record.updated_at = time.time()
        async with self._lock:
            self._data[record.name] = record

    async def delete(self, name: str) -> bool:
        async with self._lock:
            return self._data.pop(name, None) is not None

    async def list_all(self) -> list[ServiceRecord]:
        async with self._lock:
            return list(self._data.values())

    async def count(self) -> int:
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
        if not self._path.exists():
            return {}
        raw = read_yaml_file(self._path)
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
                component_id=d.get("component_id", ""),
                repo_id=d.get("repo_id", ""),
            )
        return records

    async def _save(self, records: dict[str, ServiceRecord]) -> None:
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
                "component_id": r.component_id,
                "repo_id": r.repo_id,
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(raw, default_flow_style=False), encoding="utf-8"
        )

    async def get(self, name: str) -> ServiceRecord | None:
        records = await self._load()
        return records.get(name)

    async def put(self, record: ServiceRecord) -> None:
        record.updated_at = time.time()
        async with self._lock:
            records = await self._load()
            records[record.name] = record
            await self._save(records)

    async def delete(self, name: str) -> bool:
        async with self._lock:
            records = await self._load()
            if name not in records:
                return False
            del records[name]
            await self._save(records)
            return True

    async def list_all(self) -> list[ServiceRecord]:
        records = await self._load()
        return list(records.values())

    async def count(self) -> int:
        records = await self._load()
        return len(records)
