"""Pydantic models for the onboarding pipeline: preflight inputs, derived specs, and confirm payloads."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from robotsix_central_deploy.registry.models import (
    ConfigAssistSeed,
    HealthCheck,
    PortMapping,
    ServiceConfig,
    VolumeMount,
)

# SiblingDerivedSpec is a type alias for ServiceConfig — the onboard parser
# produces the same shape as the persisted sibling model, avoiding a fragile
# duplicate field list.
SiblingDerivedSpec = ServiceConfig


class DerivedSpec(BaseModel):
    """Parsed output from a service repo's docker-compose.yml."""

    name: str = Field(description="User-supplied component slug (e.g. 'my-service')")
    git_url: str = Field(
        description="Git repository URL of the service (used to clone and parse docker-compose.yml)"
    )
    image: str = Field(
        description="Container image reference from docker-compose (e.g. 'ghcr.io/your-org/your-service:main')"
    )
    ports: list[PortMapping] = Field(
        description="Port mappings from docker-compose (container port ↔ host port)"
    )
    volume_mounts: list[VolumeMount] = Field(
        description="Named volume mounts from docker-compose (host=volume_name, named volumes only)"
    )
    env: dict[str, str] = Field(
        description="Environment variables from docker-compose; empty-string values indicate secrets, non-empty strings are preset defaults"
    )
    claude_mount: bool = Field(
        description="Whether to mount ~/.claude for Claude Code authentication (from robotsix.deploy.claude-mount label)"
    )
    claude_mount_path: str = Field(
        default="/home/app/.claude",
        description="Container path for the Claude authentication volume mount",
    )
    host_docker_sock: bool = Field(
        description="Whether to bind-mount the host Docker socket into the container (from robotsix.deploy.host-docker-sock label)"
    )
    health_check: Optional[HealthCheck] = Field(
        default=None,
        description="Container health check configuration from docker-compose (test, interval, timeout, retries, start_period)",
    )
    command: Optional[list[str]] = Field(
        default=None,
        description="Container command override from docker-compose (overrides the image's CMD)",
    )
    entrypoint: Optional[list[str]] = Field(
        default=None,
        description="Container entrypoint override from docker-compose (overrides the image's ENTRYPOINT)",
    )
    tmpfs: list[str] = Field(
        default=[],
        description="Paths to mount as tmpfs inside the container (e.g. ['/run'])",
    )
    mem_limit: str = Field(
        default="2g",
        description="Memory limit for the container (e.g. '512m', '2g')",
    )
    container_name: str = Field(
        default="",
        description="Docker container name override from docker-compose; empty means use the component id",
    )
    siblings: list[SiblingDerivedSpec] = Field(
        default=[],
        description="Additional sibling services for multi-service components (empty for single-service repos)",
    )
    config_schema: dict[str, Any] | None = Field(
        default=None,
        description="Parsed JSON Schema (config.schema.json) for the component's runtime config; null when no schema file is present",
    )
    config_example_values: dict[str, Any] | None = Field(
        default=None,
        description="Example config values from config.json or config.example.json — the deploy-default base layered under schema defaults and overridden by user input during onboard confirm; null when absent",
    )
    config_volume: Optional[str] = Field(
        default=None,
        description="Named volume that holds config.json at runtime (resolved from robotsix.deploy.config-target label)",
    )
    config_assist_command: Optional[str] = Field(
        default=None,
        description="Shell command that generates or validates config values (from robotsix.deploy.config-assist label)",
    )
    config_assist_seeds: list[ConfigAssistSeed] = Field(
        default=[],
        description="Seed field key-label pairs for the config-assist form (from robotsix.deploy.config-assist-seeds label)",
    )
    llmio_tier_level: Optional[str] = Field(
        default=None,
        description='LLM I/O tier level ("level1" through "level4") from robotsix.deploy.llmio-tier-level label; null when unset',
    )
    allow_chat_access: bool = Field(
        default=False,
        description="Whether this component is reachable via the chat-proxy gateway (from robotsix.deploy.chat-access label)",
    )
    chat_agent_mutatable: bool = Field(
        default=False,
        description="Whether the chat agent is permitted to mutate this component's configuration at runtime (from robotsix.deploy.chat-agent-mutatable label)",
    )
    user: Optional[str] = Field(
        default=None,
        description='Container user override from docker-compose (e.g. "1000:1000" for uid:gid, or "root")',
    )
    target_disk: str = Field(
        default="",
        description="Disk mount point for volume placement (e.g. '/mnt/data'); empty means use the config default or Docker's default volume location",
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
