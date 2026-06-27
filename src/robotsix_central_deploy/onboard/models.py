from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from robotsix_central_deploy.registry.models import HealthCheck, PortMapping, VolumeMount


class DerivedSpec(BaseModel):
    """Parsed output from a service repo's docker-compose.yml."""

    name: str  # user-supplied slug
    git_url: str
    image: str  # e.g. "ghcr.io/damien-robotsix/cost-monitor:main"
    ports: list[PortMapping]
    volume_mounts: list[VolumeMount]  # host=volume_name (named volumes only)
    stateful_volumes: list[str]  # volume names flagged robotsix.deploy.stateful=true
    env: dict[str, str]  # keys from compose; "" for secrets, preset string for defaults
    claude_mount: bool
    health_check: Optional[HealthCheck] = None


class ParseError(Exception):
    """Raised when compose fails deploy-contract validation."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class FetchError(Exception):
    """Raised when docker-compose.yml cannot be fetched from the git URL."""
