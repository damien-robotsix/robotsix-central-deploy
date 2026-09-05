"""Lifecycle configuration loaded from JSON via robotsix_config.

The committed ``config/config.json`` carries safe default values.
Operators replace it with a deployment-specific file containing real
secrets (``github_app_private_key``, etc.).

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

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable slug of the virtual component (lowercase, digits, hyphens).",
    )
    chat_base_url: str = Field(
        "",
        description="Base URL the chat agent uses to reach the component (e.g. http://<component>:<port>).",
    )
    chat_skill_endpoint: str = Field(
        "/chat-skill",
        description="HTTP path probed for the component's chat skill body.",
    )
    chat_skill: str = Field(
        "", description="static skill body; when non-empty, used without probing"
    )
    # --- Auth metadata for the chat agent ---
    # Scheme only.  The credential value lives in the chat agent's own
    # config (central_deploy.api_token / component_credentials.<id>).
    auth_type: str = Field("", description='"basic" | "header" | ""')
    auth_header_name: str = Field("", description='header name when auth_type="header"')


class RemoteHostEntry(BaseModel):
    """One remote Docker host that components can be deployed to.

    The host is reached over a private tunnel (e.g. WireGuard): its Docker
    API sits behind a socket proxy bound to the tunnel address, and remote
    components publish their ports on that same address so the local
    Traefik edge can dial them.
    """

    docker_url: str = Field(
        "",
        description=(
            "Docker API endpoint of the remote host, e.g. "
            "tcp://10.88.0.2:2375 (a socket proxy bound to the tunnel "
            "interface)."
        ),
    )
    reach_host: str = Field(
        "",
        description=(
            "Address the local Traefik edge dials to reach ports published "
            "on the remote host — normally the host's tunnel IP."
        ),
    )


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


class LifecycleConfig(BaseModel):
    """Configuration for the lifecycle server."""

    model_config = {"validate_assignment": True}

    # Server
    host: str = Field(  # nosec B104 — intentional bind for the containerized service
        "0.0.0.0",
        description="Interface the HTTP server binds to.",
    )
    port: int = Field(
        8100,
        description="Port the HTTP server listens on.",
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

    remote_hosts: dict[str, RemoteHostEntry] = Field(
        default_factory=dict,
        description=(
            "Remote Docker hosts components can target via their 'host' "
            "field, keyed by host name (e.g. 'bequiet'). Empty = "
            "single-host operation."
        ),
    )

    traefik_dynamic_dir: str = Field(
        "/traefik-dynamic",
        description=(
            "Directory where Traefik file-provider fragments for "
            "remote-host components are written. Must be a volume shared "
            "with the Traefik container (mounted under its "
            "providers.file.directory)."
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
        SETTINGS_DEFAULTS["disk_warn_pct"],
        description="Warn on the dashboard when free disk space drops below this percentage.",
    )
    target_disk: str = Field(
        "",
        description=(
            "Default target disk mount point for new component volumes. "
            "Empty means use Docker's default volume location. Overridden "
            "by per-deploy / onboard target-disk values."
        ),
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
        SETTINGS_DEFAULTS["registry_check_interval"],
        description=(
            "Interval (seconds) of the background update-available check; "
            "0 disables it."
        ),
    )

    system_settings_path: str = Field(
        "data/system_settings.json",
        description="Path of the operator-editable System Settings store.",
    )

    self_contract_path: str = Field(
        "deploy/docker-compose.yml",
        description=(
            "Path to central-deploy's own deploy contract (deploy/docker-compose.yml). "
            "Read at startup to seed system settings from contract labels "
            "(robotsix.deploy.settings.*)."
        ),
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
        SETTINGS_DEFAULTS["volume_audit_growth_threshold_pct"],
        description=(
            "Volume growth (percent between scans) above which a finding is raised."
        ),
    )
    volume_audit_min_delta_bytes: int = Field(
        SETTINGS_DEFAULTS["volume_audit_min_delta_bytes"],
        description=(
            "Minimum absolute growth (bytes) before a finding is raised — "
            "filters noise on small volumes. Default 10 MiB."
        ),
    )
    chat_volume_write_max_bytes: int = Field(
        1_048_576,
        description=(
            "Maximum UTF-8 byte size of a file the chat agent may write via "
            "PUT /chat/services/{name}/volumes/{vol}/files. Default 1 MiB."
        ),
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
    )
    github_app_private_key: SecretStr = Field(
        SecretStr(""),
        description="GitHub App private key (PEM) paired with github_app_id.",
    )
    installation_id: SecretStr = Field(
        SecretStr(""),
        description=(
            "GitHub App installation ID for the fleet's shared installation. "
            "Used together with github_app_id and github_app_private_key to "
            "mint short-lived installation access tokens."
        ),
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
    )

    # Langfuse auth (chat-agent "langfuse" virtual component — trace read
    # proxy).  The chat container never sees these credentials — the deploy
    # server injects Basic Auth server-side when proxying Langfuse public-API
    # requests.
    langfuse_projects: dict[str, LangfuseProjectCreds] = Field(
        default_factory=dict,
        description=(
            "Per-project Langfuse credentials.  Maps a project name to its "
            "Langfuse public and secret keys.  Operator-configured entries "
            "override auto-discovered credentials from onboarded services.  "
            'Example: {"my-project": {"public_key": "pk-...", "secret_key": "sk-..."}}.'
        ),
    )
    langfuse_base_url: str = Field(
        "",
        description="Langfuse server URL (e.g. https://langfuse.example.com).",
    )
    # OpenRouter provider keys, keyed by project name (same as
    # `langfuse_projects`).  Operator-owned bridge for components that have not
    # yet declared their own OpenRouter keys; entries here override a
    # component-declared key for the same project.
    openrouter_keys: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "OpenRouter API keys for AI model access, keyed by project name.  "
            'Example: {"my-project": "sk-or-..."}.'
        ),
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
    )
    mill_component_id: str = Field(
        SETTINGS_DEFAULTS["mill_component_id"],
        description=(
            "Component id of the mill instance onboarding registers repos with."
        ),
    )
    image_auto_prune: bool = Field(
        SETTINGS_DEFAULTS["image_auto_prune"],
        description=("After updates, remove dangling images not needed for rollback."),
    )
    llmio_tier_config: dict[str, Any] = Field(
        default=SETTINGS_DEFAULTS["llmio_tier_config"],
        description=(
            "Fleet-global llmio tier configuration, written verbatim as "
            "llmio_tier_config.json into component config volumes for "
            "robotsix-llmio's load_tier_config(). Nested shape: 'default' "
            "and 'fallback' provider slots (each binding level1-level3) "
            "plus a 'failover' policy (failure_threshold, window_seconds). "
            "Overridden by the System Settings store."
        ),
    )

    chat_agent_audit_store_path: str = Field(
        "data/chat_agent_audit.json",
        description="Path of the chat-agent mutation audit log.",
    )

    claude_auth_refresh_interval: int = Field(
        SETTINGS_DEFAULTS["claude_auth_refresh_interval"],
        description=(
            "Interval (seconds) between Claude auth credential refresh "
            "attempts; 0 disables background refresh."
        ),
    )

    # Rate limiting
    rate_limit_api_per_hour: int = Field(
        SETTINGS_DEFAULTS["rate_limit_api_per_hour"],
        description=(
            "Max API requests per IP per hour. Must accommodate the "
            "dashboard UI, which polls several endpoints every few "
            "seconds from one IP (~5000/h per open tab)."
        ),
    )

    csrf_secret: SecretStr = Field(
        SecretStr(""),
        description=(
            "Secret key for CSRF token signing. Auto-generated (random) "
            "when empty, which invalidates outstanding tokens on every "
            "restart — acceptable for single-server deployments."
        ),
    )

    # Chat agent registration toggle — when True the chat agent may register
    # new components via POST /chat/services without a pre-existing config.
    chat_agent_registration_enabled: bool = Field(
        SETTINGS_DEFAULTS["chat_agent_registration_enabled"],
        description=(
            "When True, the chat agent may register new managed components "
            "via POST /chat/services.  Registration only persists metadata — "
            "it does NOT auto-start or auto-deploy the component."
        ),
    )

    # Mobile token exchange — bearer tokens for native apps that cannot
    # use the browser SSO session-cookie flow.
    mobile_token_ttl_days: int = Field(
        SETTINGS_DEFAULTS["mobile_token_ttl_days"],
        description=(
            "Lifetime (days) of mobile bearer tokens issued by the "
            "/auth/token endpoint."
        ),
    )

    mobile_token_revocation_path: str = Field(
        "data/mobile_token_revocations.json",
        description="Path of the mobile-token revocation list.",
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
