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

from pydantic import BaseModel, Field, SecretStr

from ._settings_defaults import SETTINGS_DEFAULTS
from .models import ExecutionBackendType, StoreBackend


class VirtualComponentEntry(BaseModel):
    """Minimal spec for a virtual (non-Docker) chat-accessible component."""

    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*$")
    chat_base_url: str = ""
    chat_skill_endpoint: str = "/chat-skill"
    chat_skill: str = ""  # static skill body; when non-empty, used without probing
    # --- Auth metadata for the chat agent ---
    # "basic" → HTTP Basic Auth (username_env / password_env)
    # "header" → custom header (header_name + token_env)
    auth_type: str = ""  # "basic" | "header" | ""
    auth_header_name: str = ""  # header name when auth_type="header"
    auth_username_env: str = ""  # env var holding Basic-Auth username
    auth_password_env: SecretStr = Field(
        SecretStr(""), description="env var holding Basic-Auth password"
    )
    auth_token_env: str = ""  # env var holding a bearer/header token


class LangfuseProjectCreds(BaseModel):
    """Credentials for one Langfuse trace project."""

    public_key: str = Field(
        "",
        description="Langfuse public key for the project.",
    )
    secret_key: SecretStr = Field(
        SecretStr(""),
        description="Langfuse secret key for the project.",
    )


class OvhSftpConfig(BaseModel):
    """OVH website SFTP credentials, seeded into the encrypted env store on first boot."""

    host: str = Field(
        "",
        description="OVH SFTP hostname.",
    )
    port: int = Field(
        22,
        description="OVH SFTP port.",
    )
    user: str = Field(
        "",
        description="OVH SFTP username.",
    )
    password: SecretStr = Field(
        SecretStr(""),
        description="OVH SFTP password.",
    )


class LifecycleConfig(BaseModel):
    """Configuration for the lifecycle server."""

    model_config = {"validate_assignment": True}

    # Server
    host: str = Field(  # nosec B104 — intentional bind for the containerized service
        "0.0.0.0",
        description="Interface the HTTP server binds to.",
        json_schema_extra={"advanced": True},
    )
    port: int = Field(
        8100,
        description="Port the HTTP server listens on.",
        json_schema_extra={"advanced": True},
    )
    api_key: SecretStr = Field(
        SecretStr(""),
        description=(
            "Legacy API credential accepted via the X-API-Key header or as a "
            "Basic-auth password. Empty disables api-key auth."
        ),
    )
    auth_username: str = Field(
        SETTINGS_DEFAULTS["auth_username"],
        description=(
            "Basic-auth username for the dashboard and API. Auth is enforced "
            "only when both username and password are set (or api_key is)."
        ),
    )
    auth_password: SecretStr = Field(
        SecretStr(SETTINGS_DEFAULTS["auth_password"]),
        description="Basic-auth password paired with auth_username.",
    )

    # Persistence
    store_backend: StoreBackend = Field(
        StoreBackend.MEMORY,
        description=(
            "Persistence backend for service records: 'memory' is ephemeral "
            "(dev), 'file' persists to store_path."
        ),
        json_schema_extra={"advanced": True},
    )
    store_path: str = Field(
        "lifecycle_state.yaml",
        description="Path of the service-record store (file backend only).",
        json_schema_extra={"advanced": True},
    )

    # Execution backend
    execution_backend: ExecutionBackendType = Field(
        ExecutionBackendType.DOCKER_SDK,
        description=(
            "How containers are managed: 'docker_sdk' (full support), "
            "'docker' CLI (status/logs only), or 'noop' (dry runs)."
        ),
        json_schema_extra={"advanced": True},
    )

    component_config_store_path: str = Field(
        "data/component_configs.json",
        description="Path of the persisted per-component deployment configs.",
        json_schema_extra={"advanced": True},
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
        json_schema_extra={"advanced": True},
    )

    # Disk usage monitoring
    disk_path: str = Field(
        "/",
        description=(
            "Filesystem path whose free space is reported on the dashboard; "
            "/host_root when containerised (host filesystem mount)."
        ),
        json_schema_extra={"advanced": True},
    )
    disk_warn_pct: float = Field(
        SETTINGS_DEFAULTS["disk_warn_pct"],
        description="Warn on the dashboard when free disk space drops below this percentage.",
        json_schema_extra={"advanced": True},
    )

    # Env / secrets persistence
    env_store_path: str = Field(
        "component_env.json",
        description="Path of the per-component env/secret store.",
        json_schema_extra={"advanced": True},
    )
    secret_key_path: str = Field(
        "secrets.key",
        description="Path of the Fernet key encrypting stored secrets.",
        json_schema_extra={"advanced": True},
    )

    config_yaml_store_path: str = Field(
        "data/component_config_yaml.json",
        description="Path of the per-component config template/values store.",
        json_schema_extra={"advanced": True},
    )

    deploy_history_store_path: str = Field(
        "data/deploy_history.json",
        description="Path of the per-component deploy-history JSON store.",
        json_schema_extra={"advanced": True},
    )

    self_update_watchtower_image: str = Field(
        "containrrr/watchtower:1.7.1",
        description=(
            "One-shot updater image launched by the dashboard's "
            "'Update server' button; keep the tag pinned."
        ),
        json_schema_extra={"advanced": True},
    )

    self_update_docker_api_version: str = Field(
        "1.44",
        description=(
            "DOCKER_API_VERSION exported to the one-shot updater. Watchtower "
            "1.7.1's client defaults to API 1.25, below modern daemons' "
            "minimum, and panics without this."
        ),
        json_schema_extra={"advanced": True},
    )

    # Registry check
    registry_check_ttl: int = Field(
        300,
        description="Cache TTL (seconds) for registry manifest-digest lookups.",
        json_schema_extra={"advanced": True},
    )
    registry_check_interval: int = Field(
        SETTINGS_DEFAULTS["registry_check_interval"],
        description=(
            "Interval (seconds) of the background update-available check; "
            "0 disables it."
        ),
        json_schema_extra={"advanced": True},
    )

    system_settings_path: str = Field(
        "data/system_settings.json",
        description="Path of the operator-editable System Settings store.",
        json_schema_extra={"advanced": True},
    )

    self_contract_path: str = Field(
        "deploy/docker-compose.yml",
        description=(
            "Path to central-deploy's own deploy contract (deploy/docker-compose.yml). "
            "Read at startup to seed system settings from contract labels "
            "(robotsix.deploy.settings.*)."
        ),
        json_schema_extra={"advanced": True},
    )

    log_level: str = Field(
        SETTINGS_DEFAULTS["log_level"],
        description="Root log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    )

    gateway_base_domain: str = Field(
        SETTINGS_DEFAULTS["gateway_base_domain"],
        description=(
            "Base domain for the subdomain gateway (e.g. deploy.robotsix.net "
            "routes <component>.deploy.robotsix.net). Empty disables "
            "subdomain routing."
        ),
    )

    # Volume audit
    volume_audit_enabled: bool = Field(
        SETTINGS_DEFAULTS["volume_audit_enabled"],
        description="Master switch for the periodic volume-growth audit.",
    )
    volume_audit_interval_seconds: int = Field(
        SETTINGS_DEFAULTS["volume_audit_interval_seconds"],
        description="Interval (seconds) between volume-audit scans.",
        json_schema_extra={"advanced": True},
    )
    volume_audit_snapshot_path: str = Field(
        "data/volume_audit_snapshots.json",
        description="Path of persisted volume-size snapshots.",
        json_schema_extra={"advanced": True},
    )
    volume_audit_findings_path: str = Field(
        "data/volume_audit_findings.json",
        description="Path of persisted volume-audit findings.",
        json_schema_extra={"advanced": True},
    )
    volume_audit_growth_threshold_pct: float = Field(
        SETTINGS_DEFAULTS["volume_audit_growth_threshold_pct"],
        description=(
            "Volume growth (percent between scans) above which a finding is raised."
        ),
        json_schema_extra={"advanced": True},
    )
    volume_audit_min_delta_bytes: int = Field(
        SETTINGS_DEFAULTS["volume_audit_min_delta_bytes"],
        description=(
            "Minimum absolute growth (bytes) before a finding is raised — "
            "filters noise on small volumes. Default 10 MiB."
        ),
        json_schema_extra={"advanced": True},
    )

    # Board integration (for filing audit-finding tickets and other automations)
    board_api_url: str = Field(
        "",
        description="Board API base URL for filing audit-finding tickets; empty disables.",
        json_schema_extra={"advanced": True},
    )
    board_api_token: SecretStr = Field(
        SecretStr(""),
        description="Bearer token for the board API.",
        json_schema_extra={"advanced": True},
    )
    board_repo_id: str = Field(
        "",
        description="Board repo id under which audit tickets are filed.",
        json_schema_extra={"advanced": True},
    )

    # GitHub App auth (chat-agent "github" virtual component — GitHub Actions
    # workflow-run status). Shares the same GitHub App installation as the
    # fleet's CI/CD pipeline; the chat container never sees these credentials —
    # the deploy server mints short-lived installation tokens server-side.
    github_app_id: SecretStr = Field(
        SecretStr(""),
        description=(
            "GitHub App ID used to mint installation tokens for the chat "
            "agent's 'github' component. Empty disables the component."
        ),
        json_schema_extra={"advanced": True},
    )
    github_app_private_key: SecretStr = Field(
        SecretStr(""),
        description="GitHub App private key (PEM) paired with github_app_id.",
        json_schema_extra={"advanced": True},
    )
    installation_id: SecretStr = Field(
        SecretStr(""),
        description=(
            "GitHub App installation ID for the fleet's shared installation. "
            "Used together with github_app_id and github_app_private_key to "
            "mint short-lived installation access tokens."
        ),
        json_schema_extra={"advanced": True},
    )
    github_repo_create_token: SecretStr = Field(
        SecretStr(""),
        description=(
            "A GitHub Personal Access Token (classic 'repo' scope, or "
            "fine-grained with Administration:read-and-write) used only for "
            "POST /chat/github/repos. GitHub App installation tokens cannot "
            "create repositories under a personal account ('Resource not "
            "accessible by integration'), so repo creation needs a separate "
            "PAT. Empty disables repo creation (the "
            "Actions-status endpoints are unaffected)."
        ),
        json_schema_extra={"advanced": True},
    )

    ghcr_pull_token: SecretStr = Field(
        SecretStr(""),
        description=(
            "A GitHub Personal Access Token (classic) with ``read:packages`` "
            "scope, used to authenticate private GHCR image pulls. When set, "
            "this static token is preferred over the GitHub App installation "
            "token for ``ghcr.io`` pulls. Empty falls back to App-token auth "
            "(if configured) or anonymous pull."
        ),
        json_schema_extra={"advanced": True},
    )

    # Langfuse auth (chat-agent "langfuse" virtual component — trace read
    # proxy).  The chat container never sees these credentials — the deploy
    # server injects Basic Auth server-side when proxying Langfuse public-API
    # requests.
    langfuse_projects: dict[str, LangfuseProjectCreds] = Field(
        default_factory=dict,
        description=(
            "Langfuse project alias → credentials mapping.  Example: "
            '{"my-project": {"public_key": "pk-...", "secret_key": "sk-..."}}.'
        ),
        json_schema_extra={"advanced": True},
    )
    langfuse_base_url: str = Field(
        "",
        description="Langfuse instance base URL (no trailing slash).",
        json_schema_extra={"advanced": True},
    )

    # Caretaker
    caretaker_enabled: bool = Field(
        SETTINGS_DEFAULTS["caretaker_enabled"],
        description=(
            "Enable the periodic caretaker pass (auto-update, health, and "
            "volume checks)."
        ),
    )
    caretaker_interval_hours: int = Field(
        SETTINGS_DEFAULTS["caretaker_interval_hours"],
        description="Hours between caretaker passes.",
        json_schema_extra={"advanced": True},
    )
    mill_component_id: str = Field(
        SETTINGS_DEFAULTS["mill_component_id"],
        description=(
            "Component id of the mill instance the caretaker reports findings to."
        ),
        json_schema_extra={"advanced": True},
    )
    image_auto_prune: bool = Field(
        SETTINGS_DEFAULTS["image_auto_prune"],
        description=("After updates, remove dangling images not needed for rollback."),
        json_schema_extra={"advanced": True},
    )
    llmio_tier_config: dict[str, Any] = Field(
        default=SETTINGS_DEFAULTS["llmio_tier_config"],
        description=(
            "Fleet-global mapping from llmio capability level (level1-4) to "
            "provider and model. Overridden by the System Settings store."
        ),
        json_schema_extra={"advanced": True},
    )

    chat_agent_audit_store_path: str = Field(
        "data/chat_agent_audit.json",
        description="Path of the chat-agent mutation audit log.",
        json_schema_extra={"advanced": True},
    )

    claude_auth_refresh_interval: int = Field(
        SETTINGS_DEFAULTS["claude_auth_refresh_interval"],
        description=(
            "Interval (seconds) between Claude auth credential refresh "
            "attempts; 0 disables background refresh."
        ),
        json_schema_extra={"advanced": True},
    )

    # Rate limiting
    rate_limit_login_per_minute: int = Field(
        SETTINGS_DEFAULTS["rate_limit_login_per_minute"],
        description="Max POST /login requests per IP per minute.",
        json_schema_extra={"advanced": True},
    )
    rate_limit_api_per_hour: int = Field(
        SETTINGS_DEFAULTS["rate_limit_api_per_hour"],
        description=(
            "Max API requests per IP per hour. Must accommodate the "
            "dashboard UI, which polls several endpoints every few "
            "seconds from one IP (~5000/h per open tab)."
        ),
        json_schema_extra={"advanced": True},
    )
    rate_limit_login_max_attempts: int = Field(
        SETTINGS_DEFAULTS["rate_limit_login_max_attempts"],
        description="Failed login attempts before IP lockout.",
        json_schema_extra={"advanced": True},
    )
    rate_limit_login_lockout_seconds: int = Field(
        SETTINGS_DEFAULTS["rate_limit_login_lockout_seconds"],
        description="Lockout duration (seconds) after too many failed logins.",
        json_schema_extra={"advanced": True},
    )

    csrf_secret: SecretStr = Field(
        SecretStr(""),
        description=(
            "Secret key for CSRF token signing. Auto-generated (random) "
            "when empty, which invalidates outstanding tokens on every "
            "restart — acceptable for single-server deployments."
        ),
        json_schema_extra={"advanced": True},
    )

    # Generic deploy allowlist — component names the chat agent may deploy
    # via POST /chat/deploy even when no ComponentConfig exists yet.
    chat_agent_deployable_components: list[str] = Field(
        default_factory=list,
        description=(
            "Component names the chat agent is allowed to deploy via the "
            "generic POST /chat/deploy endpoint. Each entry must match "
            "^[a-z0-9][a-z0-9-]*$. Distinct from chat_agent_mutatable "
            "(per-component flag) — this is a server-level allowlist for "
            "components that may not have a persisted ComponentConfig yet."
        ),
    )

    # Virtual chat components
    virtual_components: list[VirtualComponentEntry] = Field(
        default_factory=list,
        description=(
            "Virtual (non-Docker) components to register in the chat-agent "
            "component roster alongside onboarded Docker services."
        ),
        json_schema_extra={"advanced": True},
    )

    # OVH SFTP credentials (seeded into encrypted env store on first boot)
    ovh_sftp: OvhSftpConfig = Field(
        default_factory=OvhSftpConfig,
        description=(
            "OVH website SFTP credentials. If all four fields are set AND "
            "the ovh-website-credentials entry does not already exist in "
            "the encrypted store, the values are seeded at startup with "
            "scope tag 'website:ovh'."
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
    def effective_chat_agent_audit_store_path(self) -> Path:
        return Path(self.chat_agent_audit_store_path)

    @property
    def auth_required(self) -> bool:
        """True when any credential set is fully configured.

        Enforced by either:
        - a non-empty ``api_key``, OR
        - a non-empty ``auth_username`` AND ``auth_password`` pair.
        """
        return bool(self.api_key.get_secret_value()) or (
            bool(self.auth_username) and bool(self.auth_password.get_secret_value())
        )
