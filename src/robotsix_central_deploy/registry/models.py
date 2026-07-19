from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PortMapping(BaseModel):
    host: int = Field(description="Host-side port number")
    container: int = Field(description="Container-side port number")
    protocol: str = Field("tcp", description="Transport protocol: 'tcp' or 'udp'")


class VolumeMount(BaseModel):
    host: str = Field(description="Host path or named-volume name")
    container: str = Field(description="Mount path inside the container")
    read_only: bool = Field(False, description="If true, mount the volume as read-only")


class HealthCheck(BaseModel):
    """Mirrors the Docker HealthCheck spec."""

    test: list[str] = Field(
        description="Health check command array, e.g. ['CMD', 'curl', '-f', 'http://localhost:8080/']"
    )
    interval_seconds: int = Field(30, description="Seconds between health check runs")
    timeout_seconds: int = Field(
        10, description="Seconds before a health check attempt times out"
    )
    retries: int = Field(
        3, description="Consecutive failures needed to mark the container unhealthy"
    )
    start_period_seconds: int = Field(
        10, description="Grace period after container start before counting failures"
    )


class ServiceConfig(BaseModel):
    """Persisted config for one non-primary (sibling) service in a multi-service component."""

    service_key: str = Field(
        description="Short key identifying this sibling service within the component"
    )
    container_name: str = Field(
        description="Docker container name for this sibling service"
    )
    image: str = Field(description="Container image reference (registry/repo:tag)")
    ports: list[PortMapping] = Field(
        default_factory=list, description="Port mappings for this sibling service"
    )
    mounts: list[VolumeMount] = Field(
        default_factory=list, description="Volume mounts for this sibling service"
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Static environment variables for this sibling service",
    )
    claude_mount: bool = Field(
        False,
        description="If true, mount ~/.claude into this sibling's container",
    )
    claude_mount_path: str = Field(
        "/home/app/.claude",
        description="Container path where the claude-auth volume is mounted",
    )
    host_docker_sock: bool = Field(
        False,
        description="If true, bind-mount the host Docker socket into this sibling",
    )
    health_check: Optional[HealthCheck] = Field(
        default=None, description="Docker health check configuration for this sibling"
    )
    command: Optional[list[str]] = Field(
        default=None, description="Override the image's default CMD"
    )
    entrypoint: Optional[list[str]] = Field(
        default=None, description="Override the image's default ENTRYPOINT"
    )
    tmpfs: list[str] = Field(
        default_factory=list,
        description="Paths to mount as tmpfs in this sibling's container (e.g. ['/run'])",
    )
    mem_limit: str = Field(
        "2g", description="Memory limit for this sibling's container (e.g. '2g')"
    )
    user: Optional[str] = Field(
        default=None, description="Container user override (e.g. '1000:1000' or 'root')"
    )


class ConfigAssistSeed(BaseModel):
    key: str = Field(description="Dotted config path, e.g. 'accounts.0.auth.username'")
    label: str | None = Field(
        default=None,
        description="Optional human-readable label; None derives from the key",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, v: object) -> object:
        if isinstance(v, str):
            return {"key": v, "label": None}
        return v


class ComponentConfig(BaseModel):
    """Declares a single managed Docker component."""

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable slug matching ^[a-z0-9][a-z0-9-]*$",
    )
    image: str = Field(description="Container image reference (registry/repo:tag)")
    container_name: str = Field(description="Docker container name on the host")
    ports: list[PortMapping] = Field(
        default_factory=list, description="Port mappings for the primary service"
    )
    mounts: list[VolumeMount] = Field(
        default_factory=list, description="Volume mounts for the primary service"
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Static environment variables for the primary service",
    )
    health_check: Optional[HealthCheck] = Field(
        default=None, description="Docker health check configuration"
    )
    claude_mount: bool = Field(
        False, description="If true, mount ~/.claude into the primary container"
    )
    claude_mount_path: str = Field(
        "/home/app/.claude",
        description="Container path where the claude-auth volume is mounted",
    )
    host_docker_sock: bool = Field(
        False, description="If true, bind-mount the host Docker socket"
    )
    named_volumes: list[str] = Field(
        default_factory=list,
        description="Named volume names to pre-create at deploy time",
    )
    siblings: list[ServiceConfig] = Field(
        default_factory=list,
        description="Additional sibling services; empty list for single-service components",
    )
    command: Optional[list[str]] = Field(
        default=None,
        description="Override the image's default CMD (from compose 'command:')",
    )
    entrypoint: Optional[list[str]] = Field(
        default=None,
        description="Override the image's default ENTRYPOINT (from compose 'entrypoint:')",
    )
    tmpfs: list[str] = Field(
        default_factory=list, description="Paths to mount as tmpfs (e.g. ['/run'])"
    )
    git_url: str = Field(
        "", description="Source repository URL recorded at onboard time"
    )
    has_config_yaml: bool = Field(
        False,
        description="True when the onboarded repo declared config/config.json",
    )
    config_volume: Optional[str] = Field(
        default=None,
        description="Named volume holding config.json, resolved from the robotsix.deploy.config-target label",
    )
    config_assist_command: Optional[str] = Field(
        default=None,
        description="Command from the robotsix.deploy.config-assist label",
    )
    config_assist_seeds: list[ConfigAssistSeed] = Field(
        default_factory=list,
        description="Seed field keys from robotsix.deploy.config-assist-seeds",
    )
    caretaker_auto_update: bool = Field(
        True, description="If true, the caretaker may auto-update this component"
    )
    repo_id: str = Field(
        "", description="Unique repository identifier in the fleet registry"
    )
    mem_limit: str = Field(
        "2g", description="Memory limit for the primary container (e.g. '2g')"
    )
    user: Optional[str] = Field(
        default=None,
        description="Container user override (e.g. '1000:1000' or 'root')",
    )
    llmio_tier_level: Optional[str] = Field(
        default=None,
        description="Capability tier: 'level1', 'level2', 'level3', or 'level4'",
    )
    allow_chat_access: bool = Field(
        False,
        description="If true, component exposes GET /chat-skill for the chat agent",
    )
    chat_agent_mutatable: bool = Field(
        False,
        description="If true, the chat agent may restart, deploy, or update config for this component",
    )
    is_virtual: bool = Field(
        False,
        description="If true, non-Docker component; never gets a ServiceRecord or dashboard row",
    )
    chat_base_url: str | None = Field(
        default=None,
        description="Base URL override for virtual (non-Docker) components; None derives from container:port",
    )
    chat_skill_endpoint: str = Field(
        "/chat-skill",
        description="Endpoint the roster probe hits for the skill body",
    )
    chat_skill: str = Field(
        "",
        description="Static skill body; when non-empty, used directly without probing",
    )
    # --- Auth metadata for the chat agent ---
    auth_type: str = Field(
        "", description="Authentication type: 'basic', 'header', or '' (none)"
    )
    auth_header_name: str = Field(
        "", description="Header name when auth_type is 'header'"
    )
    auth_username_env: str = Field(
        "", description="Environment variable holding the Basic-Auth username"
    )
    auth_password_env: str = Field(
        "", description="Environment variable holding the Basic-Auth password"
    )
    auth_token_env: str = Field(
        "", description="Environment variable holding a bearer/header token"
    )
    # --- Credential scoping ---
    consumed_scopes: list[str] = Field(
        default_factory=list,
        description="Scope glob patterns (e.g. 'website:ovh'), resolved at deploy time",
    )
