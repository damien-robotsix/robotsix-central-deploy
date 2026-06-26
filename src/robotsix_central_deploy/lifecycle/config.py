"""Lifecycle configuration loaded from environment variables.

All settings are prefixed with ``ROBOTSIX_LIFECYCLE_``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class LifecycleConfig(BaseSettings):
    """Configuration for the lifecycle server."""

    model_config = SettingsConfigDict(
        env_prefix="ROBOTSIX_LIFECYCLE_",
        env_file=".env.lifecycle",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8100
    api_key: str = ""

    # Persistence
    store_backend: str = "memory"  # "memory" | "file"
    store_path: str = "lifecycle_state.yaml"

    # Execution backend
    execution_backend: str = "docker_sdk"  # "docker_sdk" | "docker" | "noop"

    # Component registry
    registry_path: str = "config/components.yaml"

    @property
    def effective_store_path(self) -> Path:
        return Path(self.store_path)

    @property
    def effective_registry_path(self) -> Path:
        return Path(self.registry_path)

    @property
    def auth_required(self) -> bool:
        """True when an API key is configured — auth is optional for dev."""
        return bool(self.api_key)
