"""Lifecycle configuration loaded from JSON via robotsix_config.

The committed ``config/config.json`` carries safe default values.
Operators replace it with a deployment-specific file containing real
secrets (``api_key``, ``auth_password``, ``board_api_token``, etc.).

Field descriptions are surfaced in ``config/config.schema.json`` (kept in
sync by the CI drift check) and rendered as help bubbles by the deploy UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import ExecutionBackendType, StoreBackend


class LifecycleConfig(BaseModel):
    """Configuration for the lifecycle server."""

    # Server
    host: str = Field(  # nosec B104 — intentional bind for the containerized service
        "0.0.0.0",
        description="Interface the HTTP server binds to.",
    )
    port: int = Field(8100, description="Port the HTTP server listens on.")
    api_key: str = Field(
        "",
        description=(
            "Legacy API credential accepted via the X-API-Key header or as a "
            "Basic-auth password. Empty disables api-key auth."
        ),
    )
    auth_username: str = Field(
        "",
        description=(
            "Basic-auth username for the dashboard and API. Auth is enforced "
            "only when both username and password are set (or api_key is)."
        ),
    )
    auth_password: str = Field(
        "",
        description="Basic-auth password paired with auth_username.",
    )

    # Persistence
    store_backend: StoreBackend = Field(
        StoreBackend.MEMORY,
        description=(
            "Persistence backend for service records: 'memory' is ephemeral "
            "(dev), 'file' persists to store_path."
        ),
    )
    store_path: str = Field(
        "lifecycle_state.yaml",
        description="Path of the service-record store (file backend only).",
    )

    # Execution backend
    execution_backend: ExecutionBackendType = Field(
        ExecutionBackendType.DOCKER_SDK,
        description=(
            "How containers are managed: 'docker_sdk' (full support), "
            "'docker' CLI (status/logs only), or 'noop' (dry runs)."
        ),
    )

    component_config_store_path: str = Field(
        "data/component_configs.json",
        description="Path of the persisted per-component deployment configs.",
    )

    docker_socket_url: str = Field(
        "unix:///var/run/docker.sock",
        description=(
            "Docker API endpoint. Production runs behind a socket proxy: "
            "tcp://socket-proxy:2375."
        ),
    )

    docker_sdk_timeout: int = Field(
        120,
        description=(
            "Client-level timeout (seconds) for every Docker SDK operation "
            "(pull, create, start, stop, …). The default accommodates "
            "typical image pulls."
        ),
    )

    # Disk usage monitoring
    disk_path: str = Field(
        "/",
        description=(
            "Filesystem path whose free space is reported on the dashboard; "
            "/host_root when containerised (host filesystem mount)."
        ),
    )
    disk_warn_pct: float = Field(
        10.0,
        description="Warn on the dashboard when free disk space drops below this percentage.",
    )

    # Env / secrets persistence
    env_store_path: str = Field(
        "component_env.json",
        description="Path of the per-component env/secret store.",
    )
    secret_key_path: str = Field(
        "secrets.key",
        description="Path of the Fernet key encrypting stored secrets.",
    )

    config_yaml_store_path: str = Field(
        "data/component_config_yaml.json",
        description="Path of the per-component config template/values store.",
    )

    deploy_history_store_path: str = Field(
        "data/deploy_history.json",
        description="Path of the per-component deploy-history JSON store.",
    )

    self_update_watchtower_image: str = Field(
        "containrrr/watchtower:1.7.1",
        description=(
            "One-shot updater image launched by the dashboard's "
            "'Update server' button; keep the tag pinned."
        ),
    )

    self_update_docker_api_version: str = Field(
        "1.44",
        description=(
            "DOCKER_API_VERSION exported to the one-shot updater. Watchtower "
            "1.7.1's client defaults to API 1.25, below modern daemons' "
            "minimum, and panics without this."
        ),
    )

    # Registry check
    registry_check_ttl: int = Field(
        300,
        description="Cache TTL (seconds) for registry manifest-digest lookups.",
    )
    registry_check_interval: int = Field(
        300,
        description=(
            "Interval (seconds) of the background update-available check; "
            "0 disables it."
        ),
    )

    system_settings_path: str = Field(
        "data/system_settings.json",
        description="Path of the operator-editable System Settings store.",
    )

    log_level: str = Field(
        "INFO",
        description="Root log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    )

    gateway_base_domain: str = Field(
        "",
        description=(
            "Base domain for the subdomain gateway (e.g. deploy.robotsix.net "
            "routes <component>.deploy.robotsix.net). Empty disables "
            "subdomain routing."
        ),
    )

    # Volume audit
    volume_audit_enabled: bool = Field(
        False,
        description="Master switch for the periodic volume-growth audit.",
    )
    volume_audit_interval_seconds: int = Field(
        3600,
        description="Interval (seconds) between volume-audit scans.",
    )
    volume_audit_snapshot_path: str = Field(
        "data/volume_audit_snapshots.json",
        description="Path of persisted volume-size snapshots.",
    )
    volume_audit_findings_path: str = Field(
        "data/volume_audit_findings.json",
        description="Path of persisted volume-audit findings.",
    )
    volume_audit_growth_threshold_pct: float = Field(
        10.0,
        description=(
            "Volume growth (percent between scans) above which a finding is raised."
        ),
    )
    volume_audit_min_delta_bytes: int = Field(
        10_485_760,
        description=(
            "Minimum absolute growth (bytes) before a finding is raised — "
            "filters noise on small volumes. Default 10 MiB."
        ),
    )

    # Board integration (for filing audit-finding tickets and other automations)
    board_api_url: str = Field(
        "",
        description="Board API base URL for filing audit-finding tickets; empty disables.",
    )
    board_api_token: str = Field(
        "",
        description="Bearer token for the board API.",
    )
    board_repo_id: str = Field(
        "",
        description="Board repo id under which audit tickets are filed.",
    )

    # Caretaker
    caretaker_enabled: bool = Field(
        False,
        description=(
            "Enable the periodic caretaker pass (auto-update, health, and "
            "volume checks)."
        ),
    )
    caretaker_interval_hours: int = Field(
        24,
        description="Hours between caretaker passes.",
    )
    mill_component_id: str = Field(
        "mill",
        description=(
            "Component id of the mill instance the caretaker reports findings to."
        ),
    )
    image_auto_prune: bool = Field(
        False,
        description=("After updates, remove dangling images not needed for rollback."),
    )
    llmio_tier_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Fleet-global mapping from llmio capability level (level1-4) to "
            "provider and model. Overridden by the System Settings store."
        ),
    )

    claude_auth_refresh_interval: int = Field(
        1800,
        description=(
            "Interval (seconds) between Claude auth credential refresh "
            "attempts; 0 disables background refresh."
        ),
    )

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
    def effective_deploy_history_store_path(self) -> Path:
        return Path(self.deploy_history_store_path)

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
