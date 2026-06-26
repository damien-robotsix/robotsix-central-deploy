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
    auth_username: str = ""  # ROBOTSIX_LIFECYCLE_AUTH_USERNAME
    auth_password: str = ""  # ROBOTSIX_LIFECYCLE_AUTH_PASSWORD

    # Persistence
    store_backend: str = "memory"  # "memory" | "file"
    store_path: str = "lifecycle_state.yaml"

    # Execution backend
    execution_backend: str = "docker_sdk"  # "docker_sdk" | "docker" | "noop"

    # Component registry
    registry_path: str = "config/components.yaml"

    # Docker socket URL (env: ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL)
    # Production value: tcp://socket-proxy:2375
    docker_socket_url: str = "unix:///var/run/docker.sock"

    @property
    def effective_store_path(self) -> Path:
        return Path(self.store_path)

    @property
    def effective_registry_path(self) -> Path:
        return Path(self.registry_path)

    @property
    def auth_required(self) -> bool:
        """True when credentials are configured — auth is optional for dev.

        Only ``api_key`` gates enforcement.  The legacy ``auth_username`` /
        ``auth_password`` fields are no longer consulted because ``verify_auth``
        matches passwords against ``api_key`` alone.  A user with only those
        legacy vars set would otherwise be locked out with no valid credential.
        """
        return bool(self.api_key)
