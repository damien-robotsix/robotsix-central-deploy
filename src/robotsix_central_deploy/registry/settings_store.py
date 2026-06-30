"""System-wide operator-configurable settings — JSON-backed persistent store.

Mirrors ``ComponentConfigStore`` in design: async lock, corruption guard,
parent-directory creation before write.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class SystemSettings(BaseModel):
    """Operator-configurable runtime settings for central-deploy."""

    ghcr_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_pct: float = 10.0  # % free
    registry_check_interval: int = 300  # seconds; 0 = disabled
    log_level: str = "INFO"
    gateway_base_domain: str = ""  # e.g. "deploy.robotsix.net"
    claude_host_mount_path: str = (
        ""  # e.g. "/home/operator/.claude"; empty = use ~/.claude
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        normalised = v.upper()
        if normalised not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Unknown log level '{v}'. Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
            )
        return normalised


class SystemSettingsStore:
    """Persists ``SystemSettings`` to a JSON file on disk.

    Supports concurrent async access via an ``asyncio.Lock``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers (synchronous — always called under the lock)
    # ------------------------------------------------------------------

    def _load(self) -> SystemSettings:
        if not self._path.exists():
            return SystemSettings()
        try:
            raw: dict[str, object] = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "SystemSettingsStore: failed to read %s — %s; treating store as empty",
                self._path,
                exc,
            )
            return SystemSettings()
        return SystemSettings.model_validate(raw)

    def _save(self, settings: SystemSettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(settings.model_dump(), indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self) -> SystemSettings:
        async with self._lock:
            return self._load()

    async def put(self, settings: SystemSettings) -> None:
        async with self._lock:
            self._save(settings)

    # ------------------------------------------------------------------
    # Overlay stored settings onto a LifecycleConfig
    # ------------------------------------------------------------------

    def overlay(self, config: Any) -> Any:
        """Return a *copy* of *config* with every stored setting overlaid.

        All stored values take precedence over env-var defaults — an entry
        in the store represents an intentional operator choice.

        When the settings file does not exist yet (first boot), the config
        is returned unchanged so that ``ROBOTSIX_LIFECYCLE_*`` environment
        variables are preserved.
        """
        if not self._path.exists():
            return config

        stored = self._load()
        return config.model_copy(
            update={
                "ghcr_token": stored.ghcr_token,
                "auth_username": stored.auth_username,
                "auth_password": stored.auth_password,
                "disk_warn_pct": stored.disk_warn_pct,
                "registry_check_interval": stored.registry_check_interval,
                "log_level": stored.log_level,
                "gateway_base_domain": stored.gateway_base_domain,
                "claude_host_mount_path": stored.claude_host_mount_path,
            }
        )
