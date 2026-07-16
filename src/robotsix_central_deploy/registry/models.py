from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PortMapping(BaseModel):
    host: int
    container: int
    protocol: str = "tcp"  # "tcp" | "udp"


class VolumeMount(BaseModel):
    host: str  # host path or named-volume name
    container: str  # mount path inside container
    read_only: bool = False


class HealthCheck(BaseModel):
    """Mirrors the Docker HealthCheck spec."""

    test: list[str]  # e.g. ["CMD", "curl", "-f", "http://localhost:8080/"]
    interval_seconds: int = 30
    timeout_seconds: int = 10
    retries: int = 3
    start_period_seconds: int = 10


class ServiceConfig(BaseModel):
    """Persisted config for one non-primary (sibling) service in a multi-service component."""

    service_key: str
    container_name: str
    image: str
    ports: list[PortMapping] = []
    mounts: list[VolumeMount] = []
    env: dict[str, str] = {}
    claude_mount: bool = False
    claude_mount_path: str = "/home/app/.claude"  # container path of the claude-auth volume; must match the image user's $HOME/.claude
    host_docker_sock: bool = False
    health_check: Optional[HealthCheck] = None
    command: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None
    tmpfs: list[str] = []  # paths to mount as tmpfs (e.g. ["/run"])
    mem_limit: str = "2g"
    user: Optional[str] = None  # container user override (e.g. "1000:1000" or "root")


class ConfigAssistSeed(BaseModel):
    key: str  # dotted config path, e.g. "accounts.0.auth.username"
    label: str | None = None  # optional human-readable label; None → derive from key

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, v: object) -> object:
        if isinstance(v, str):
            return {"key": v, "label": None}
        return v


class ComponentConfig(BaseModel):
    """Declares a single managed Docker component."""

    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*$")  # stable slug
    image: str  # repo:tag — the target image to run
    container_name: str  # Docker container name on the host
    ports: list[PortMapping] = []
    mounts: list[VolumeMount] = []
    env: dict[str, str] = {}
    health_check: Optional[HealthCheck] = None
    claude_mount: bool = False
    claude_mount_path: str = "/home/app/.claude"  # container path of the claude-auth volume; must match the image user's $HOME/.claude
    host_docker_sock: bool = False
    named_volumes: list[str] = []  # volume names to pre-create at deploy time
    siblings: list[ServiceConfig] = []  # empty = single-service (backward compat)
    command: Optional[list[str]] = None  # container command (from compose 'command:')
    entrypoint: Optional[list[str]] = (
        None  # container entrypoint (from compose 'entrypoint:')
    )
    tmpfs: list[str] = []  # paths to mount as tmpfs (e.g. ["/run"])
    git_url: str = ""  # source repo URL from onboard
    has_config_yaml: bool = False  # True when the repo declared config/config.json
    config_volume: Optional[str] = (
        None  # named volume that holds config.json (resolved from robotsix.deploy.config-target label)
    )
    config_assist_command: Optional[str] = (
        None  # command from robotsix.deploy.config-assist
    )
    config_assist_seeds: list[
        ConfigAssistSeed
    ] = []  # seed field keys from robotsix.deploy.config-assist-seeds
    caretaker_auto_update: bool = True
    repo_id: str = ""
    mem_limit: str = "2g"
    user: Optional[str] = None  # container user override (e.g. "1000:1000" or "root")
    llmio_tier_level: Optional[str] = (
        None  # "level1" | "level2" | "level3" | "level4" — which capability tier
    )
    allow_chat_access: bool = (
        False  # true = component exposes GET /chat-skill for the chat agent
    )
    is_virtual: bool = False  # true = non-Docker component; must never get a ServiceRecord/dashboard row
    chat_base_url: str | None = (
        None  # override base URL for virtual (non-Docker) components; None → derived from container:port
    )
    chat_skill_endpoint: str = (
        "/chat-skill"  # endpoint the roster endpoint probes for the skill body
    )
    chat_skill: str = (
        ""  # static skill body; when non-empty, used directly without probing
    )
    # --- Auth metadata for the chat agent ---
    auth_type: str = ""  # "basic" | "header" | ""
    auth_header_name: str = ""  # header name when auth_type="header"
    auth_username_env: str = ""  # env var holding Basic-Auth username
    auth_password_env: str = ""  # env var holding Basic-Auth password
    auth_token_env: str = ""  # env var holding a bearer/header token
