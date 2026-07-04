from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from robotsix_central_deploy.registry.models import (
    ConfigAssistSeed,
    HealthCheck,
    PortMapping,
    VolumeMount,
)


class SiblingDerivedSpec(BaseModel):
    """Parsed config for a non-primary service in a multi-service compose."""

    service_key: str  # compose services: key (e.g. "ingester")
    container_name: (
        str  # derived name ("<component>-<service_key>") or container_name: override
    )
    image: str
    ports: list[PortMapping] = []
    volume_mounts: list[VolumeMount] = []
    env: dict[str, str] = {}
    claude_mount: bool = False
    host_docker_sock: bool = False
    health_check: Optional[HealthCheck] = None
    command: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None


class DerivedSpec(BaseModel):
    """Parsed output from a service repo's docker-compose.yml."""

    name: str  # user-supplied slug
    git_url: str
    image: str  # e.g. "ghcr.io/your-org/your-service:main"
    ports: list[PortMapping]
    volume_mounts: list[VolumeMount]  # host=volume_name (named volumes only)
    env: dict[str, str]  # keys from compose; "" for secrets, preset string for defaults
    claude_mount: bool
    host_docker_sock: bool
    health_check: Optional[HealthCheck] = None
    command: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None
    container_name: str = ""  # override from compose; empty means "use spec.name"
    siblings: list[SiblingDerivedSpec] = []  # empty for single-service repos
    config_schema: dict[str, Any] | None = (
        None  # parsed config/config.json, null when absent
    )
    config_example_values: dict[str, Any] | None = (
        None  # parsed config/config.example.json (or committed config/config.json)
        # values — the "deploy default" base layered under schema defaults and
        # over-ridden by user form input during onboard confirm; null when absent
    )
    config_volume: Optional[str] = (
        None  # named volume that holds config.json (resolved from robotsix.deploy.config-target label)
    )
    config_assist_command: Optional[str] = (
        None  # shell command from robotsix.deploy.config-assist
    )
    config_assist_seeds: list[
        ConfigAssistSeed
    ] = []  # seed field keys from robotsix.deploy.config-assist-seeds
    llmio_tier_level: Optional[str] = (
        None  # "level1"–"level4" from robotsix.deploy.llmio-tier-level label
    )


class ParseError(Exception):
    """Raised when compose fails deploy-contract validation."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class FetchError(Exception):
    """Raised when docker-compose.yml cannot be fetched from the git URL."""


class ConfigParseError(Exception):
    """Raised when config/config.json cannot be parsed."""
