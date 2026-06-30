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
    host: str = "0.0.0.0"  # nosec B104 — intentional bind for the containerized service
    port: int = 8100
    api_key: str = ""
    auth_username: str = ""  # ROBOTSIX_LIFECYCLE_AUTH_USERNAME
    auth_password: str = ""  # ROBOTSIX_LIFECYCLE_AUTH_PASSWORD
    # Persistence
    store_backend: str = "memory"  # "memory" | "file"
    store_path: str = "lifecycle_state.yaml"

    # Execution backend
    execution_backend: str = "docker_sdk"  # "docker_sdk" | "docker" | "noop"

    # Dynamic component config store
    component_config_store_path: str = "data/component_configs.json"
    # env var: ROBOTSIX_LIFECYCLE_COMPONENT_CONFIG_STORE_PATH

    # Docker socket URL (env: ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL)
    # Production value: tcp://socket-proxy:2375
    docker_socket_url: str = "unix:///var/run/docker.sock"

    # Disk usage monitoring
    disk_path: str = (
        "/"  # env: ROBOTSIX_LIFECYCLE_DISK_PATH — /host_root when containerised
    )
    disk_warn_percent: float = 10.0  # env: ROBOTSIX_LIFECYCLE_DISK_WARN_PERCENT

    # Env / secrets persistence
    env_store_path: str = "component_env.json"  # ROBOTSIX_LIFECYCLE_ENV_STORE_PATH
    secret_key_path: str = "secrets.key"  # ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH

    # Per-component config.yaml store
    config_yaml_store_path: str = "data/component_config_yaml.json"
    # env: ROBOTSIX_LIFECYCLE_CONFIG_YAML_STORE_PATH

    # Registry check
    ghcr_token: str = ""  # ROBOTSIX_LIFECYCLE_GHCR_TOKEN
    registry_check_ttl: int = (
        300  # ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL  (cache TTL, seconds)
    )
    registry_check_interval: int = 300  # ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL (bg task interval; 0 = disabled)

    # Settings store
    system_settings_path: str = "data/system_settings.json"
    # env: ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH

    # Logging
    log_level: str = "INFO"  # env: ROBOTSIX_LIFECYCLE_LOG_LEVEL

    # Gateway
    gateway_base_domain: str = ""  # ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN

    # Claude mount
    claude_host_mount_path: str = ""  # ROBOTSIX_LIFECYCLE_CLAUDE_HOST_MOUNT_PATH

    # Volume audit
    volume_audit_enabled: bool = False
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_ENABLED — master on/off switch; default OFF

    volume_audit_interval_seconds: int = 3600
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_INTERVAL_SECONDS

    volume_audit_snapshot_path: str = "data/volume_audit_snapshots.json"
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_SNAPSHOT_PATH

    volume_audit_findings_path: str = "data/volume_audit_findings.json"
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_FINDINGS_PATH

    volume_audit_growth_threshold_pct: float = 10.0
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_GROWTH_THRESHOLD_PCT

    volume_audit_min_delta_bytes: int = 10_485_760
    # ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_MIN_DELTA_BYTES (default 10 MiB)

    @property
    def effective_store_path(self) -> Path:
        return Path(self.store_path)

    @property
    def effective_component_config_store_path(self) -> Path:
        return Path(self.component_config_store_path)

    @property
    def effective_system_settings_path(self) -> Path:
        return Path(self.system_settings_path)

    @property
    def auth_required(self) -> bool:
        """True when any credential set is fully configured.

        Enforced by either:
        - a non-empty ``api_key``, OR
        - a non-empty ``auth_username`` AND ``auth_password`` pair.
        """
        return bool(self.api_key) or (
            bool(self.auth_username) and bool(self.auth_password)
        )
