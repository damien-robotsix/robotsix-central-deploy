"""Persistent store for dynamically-onboarded ComponentConfig entries.

Provides a JSON-backed store with async serialisation safety.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from robotsix_central_deploy.registry.models import ComponentConfig

logger = logging.getLogger(__name__)


class ComponentConfigStore:
    """Persists ``ComponentConfig`` entries to a JSON file on disk.

    Designed for concurrent access from async handlers — writes are
    serialised via an ``asyncio.Lock``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, ComponentConfig]:
        if not self._path.exists():
            return {}
        try:
            raw: dict = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "ComponentConfigStore: failed to read %s — %s; treating store as empty",
                self._path,
                exc,
            )
            return {}
        return {k: ComponentConfig.model_validate(v) for k, v in raw.items()}

    def _save(self, configs: dict[str, ComponentConfig]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({k: v.model_dump() for k, v in configs.items()}, indent=2),
            encoding="utf-8",
        )

    def get(self, id: str) -> Optional[ComponentConfig]:
        return self._load().get(id)

    async def put(self, config: ComponentConfig) -> None:
        async with self._lock:
            configs = self._load()
            configs[config.id] = config
            self._save(configs)

    async def delete(self, id: str) -> bool:
        async with self._lock:
            configs = self._load()
            if id not in configs:
                return False
            del configs[id]
            self._save(configs)
            return True

    def all(self) -> list[ComponentConfig]:
        return list(self._load().values())
