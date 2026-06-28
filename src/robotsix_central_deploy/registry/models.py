from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PortMapping(BaseModel):
    host: int
    container: int
    protocol: str = "tcp"  # "tcp" | "udp"


class VolumeMount(BaseModel):
    host: str          # host path or named-volume name
    container: str     # mount path inside container
    read_only: bool = False


class HealthCheck(BaseModel):
    """Mirrors the Docker HealthCheck spec."""

    test: list[str]              # e.g. ["CMD", "curl", "-f", "http://localhost:8080/"]
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
    health_check: Optional[HealthCheck] = None
    command: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None


class ComponentConfig(BaseModel):
    """Declares a single managed Docker component."""

    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*$")  # stable slug
    image: str                    # repo:tag — the target image to run
    container_name: str           # Docker container name on the host
    ports: list[PortMapping] = []
    mounts: list[VolumeMount] = []
    env: dict[str, str] = {}
    health_check: Optional[HealthCheck] = None
    claude_mount: bool = False
    named_volumes: list[str] = []      # volume names to pre-create at deploy time
    stateful_volumes: list[str] = []   # subset with robotsix.deploy.stateful label (informational)
    siblings: list[ServiceConfig] = []  # empty = single-service (backward compat)
    command: Optional[list[str]] = None  # container command (from compose 'command:')
    entrypoint: Optional[list[str]] = None  # container entrypoint (from compose 'entrypoint:')
    git_url: str = ""                   # source repo URL from onboard
    has_config_yaml: bool = False       # True when the repo declared config/config.yaml
    config_volume: Optional[str] = None  # named volume that holds config.yaml (resolved from robotsix.deploy.config-target label)
    config_assist_command: Optional[str] = None   # command from robotsix.deploy.config-assist
    config_assist_seeds: list[str] = []           # seed field keys from robotsix.deploy.config-assist-seeds
