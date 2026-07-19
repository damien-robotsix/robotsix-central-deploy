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

from ..lifecycle._settings_defaults import SETTINGS_DEFAULTS

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class SystemSettings(BaseModel):
    """Operator-configurable runtime settings for central-deploy."""

    # All defaults sourced from SETTINGS_DEFAULTS — the single source of truth
    # shared with LifecycleConfig.  See lifecycle/_settings_defaults.py.
    auth_username: str = SETTINGS_DEFAULTS["auth_username"]
    auth_password: str = SETTINGS_DEFAULTS["auth_password"]
    disk_warn_pct: float = SETTINGS_DEFAULTS["disk_warn_pct"]  # % free
    registry_check_interval: int = SETTINGS_DEFAULTS[
        "registry_check_interval"
    ]  # seconds; 0 = disabled
    log_level: str = SETTINGS_DEFAULTS["log_level"]
    gateway_base_domain: str = SETTINGS_DEFAULTS[
        "gateway_base_domain"
    ]  # e.g. "deploy.robotsix.net"
    caretaker_enabled: bool = SETTINGS_DEFAULTS["caretaker_enabled"]
    caretaker_interval_hours: int = SETTINGS_DEFAULTS["caretaker_interval_hours"]
    mill_component_id: str = SETTINGS_DEFAULTS[
        "mill_component_id"
    ]  # component id the caretaker reports to
    image_auto_prune: bool = SETTINGS_DEFAULTS[
        "image_auto_prune"
    ]  # prune dangling images after updates
    llmio_tier_config: dict[str, Any] = SETTINGS_DEFAULTS["llmio_tier_config"]
    claude_auth_refresh_interval: int = SETTINGS_DEFAULTS[
        "claude_auth_refresh_interval"
    ]  # seconds; 0 = disabled
    rate_limit_login_per_minute: int = SETTINGS_DEFAULTS["rate_limit_login_per_minute"]
    rate_limit_api_per_hour: int = SETTINGS_DEFAULTS["rate_limit_api_per_hour"]
    rate_limit_login_max_attempts: int = SETTINGS_DEFAULTS[
        "rate_limit_login_max_attempts"
    ]
    rate_limit_login_lockout_seconds: int = SETTINGS_DEFAULTS[
        "rate_limit_login_lockout_seconds"
    ]
    volume_audit_enabled: bool = SETTINGS_DEFAULTS["volume_audit_enabled"]
    volume_audit_interval_seconds: int = SETTINGS_DEFAULTS[
        "volume_audit_interval_seconds"
    ]
    volume_audit_growth_threshold_pct: float = SETTINGS_DEFAULTS[
        "volume_audit_growth_threshold_pct"
    ]
    volume_audit_min_delta_bytes: int = SETTINGS_DEFAULTS[
        "volume_audit_min_delta_bytes"
    ]

    @field_validator("volume_audit_interval_seconds")
    @classmethod
    def _validate_volume_audit_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("volume_audit_interval_seconds must be >= 1")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        normalised = v.upper()
        if normalised not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Unknown log level '{v}'. Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
            )
        return normalised

    @field_validator("caretaker_interval_hours")
    @classmethod
    def _validate_caretaker_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("caretaker_interval_hours must be >= 1")
        return v

    @field_validator("mill_component_id")
    @classmethod
    def _validate_mill_component_id(cls, v: str) -> str:
        return v.strip()


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

        The overlay field set is driven by ``SETTINGS_DEFAULTS`` keys, so
        adding a shared field to ``_settings_defaults.py`` automatically
        includes it in the overlay without a separate manual sync step.
        """
        if not self._path.exists():
            return config

        stored = self._load()
        return config.model_copy(
            update={key: getattr(stored, key) for key in SETTINGS_DEFAULTS}
        )
