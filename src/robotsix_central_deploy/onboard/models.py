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
        description="Git URL of the service repository containing docker-compose.yml"
    )
    image: str = Field(
        description="Container image reference (e.g. 'ghcr.io/org/service:main')"
    )
    ports: list[PortMapping] = Field(
        description="Port mappings extracted from docker-compose.yml"
    )
    volume_mounts: list[VolumeMount] = Field(
        description="Named volume mounts extracted from docker-compose.yml"
    )
    env: dict[str, str] = Field(
        description="Environment variables from docker-compose.yml; empty string values denote secrets to be supplied by the operator"
    )
    claude_mount: bool = Field(
        description="Whether to bind-mount ~/.claude into the container for Claude Code access"
    )
    claude_mount_path: str = Field(
        "/home/app/.claude",
        description="Container path for the claude-auth volume, from robotsix.deploy.claude-mount-path label",
    )
    host_docker_sock: bool = Field(
        description="Whether the container needs access to the host Docker socket"
    )
    health_check: Optional[HealthCheck] = Field(
        default=None,
        description="Container health check configuration from docker-compose.yml",
    )
    command: Optional[list[str]] = Field(
        default=None,
        description="Override the container's default command",
    )
    entrypoint: Optional[list[str]] = Field(
        default=None,
        description="Override the container's default entrypoint",
    )
    tmpfs: list[str] = Field(
        default=[],
        description="Paths to mount as tmpfs inside the container (e.g. ['/run'])",
    )
    mem_limit: str = Field(
        default="2g",
        description="Memory limit for the container (e.g. '2g', '512m')",
    )
    container_name: str = Field(
        default="",
        description="Explicit container name; empty string means derive from component id",
    )
    siblings: list[SiblingDerivedSpec] = Field(
        default_factory=list,
        description="Additional sibling services for multi-service docker-compose repos",
    )
    config_schema: dict[str, Any] | None = Field(
        default=None,
        description="Parsed config/config.json JSON Schema for the component's runtime settings; None when absent",
    )
    config_example_values: dict[str, Any] | None = Field(
        default=None,
        description="Default config values from config/config.example.json, used as the deploy baseline",
    )
    config_volume: Optional[str] = Field(
        default=None,
        description="Named volume that stores the component's config.json, resolved from robotsix.deploy.config-target label",
    )
    config_assist_command: Optional[str] = Field(
        default=None,
        description="Shell command from robotsix.deploy.config-assist label, used to populate initial config values",
    )
    config_assist_seeds: list[ConfigAssistSeed] = Field(
        default=[],
        description="Seed field keys from robotsix.deploy.config-assist-seeds label",
    )
    llmio_tier_level: Optional[str] = Field(
        default=None,
        description="LLM I/O tier level ('level1'–'level4') from robotsix.deploy.llmio-tier-level label",
    )
    allow_chat_access: bool = Field(
        default=False,
        description="Whether the chat service can route requests to this component, from robotsix.deploy.chat-access label",
    )
    chat_agent_mutatable: bool = Field(
        default=False,
        description="Whether the chat agent can mutate this component's settings, from robotsix.deploy.chat-agent-mutatable label",
    )
    user: Optional[str] = Field(
        default=None,
        description="Container user override (e.g. '1000:1000' or 'root'), from docker-compose.yml",
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
