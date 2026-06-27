from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from robotsix_central_deploy.registry.models import HealthCheck, PortMapping, VolumeMount


class SiblingDerivedSpec(BaseModel):
    """Parsed config for a non-primary service in a multi-service compose."""

    service_key: str       # compose services: key (e.g. "ingester")
    container_name: str    # derived name ("<component>-<service_key>") or container_name: override
    image: str
    ports: list[PortMapping] = []
    volume_mounts: list[VolumeMount] = []
    env: dict[str, str] = {}
    claude_mount: bool = False
    health_check: Optional[HealthCheck] = None


class DerivedSpec(BaseModel):
    """Parsed output from a service repo's docker-compose.yml."""

    name: str  # user-supplied slug
    git_url: str
    image: str  # e.g. "ghcr.io/your-org/your-service:main"
    ports: list[PortMapping]
    volume_mounts: list[VolumeMount]  # host=volume_name (named volumes only)
    stateful_volumes: list[str]  # volume names flagged robotsix.deploy.stateful=true
    env: dict[str, str]  # keys from compose; "" for secrets, preset string for defaults
    claude_mount: bool
    health_check: Optional[HealthCheck] = None
    container_name: str = ""  # override from compose; empty means "use spec.name"
    siblings: list[SiblingDerivedSpec] = []  # empty for single-service repos


class ParseError(Exception):
    """Raised when compose fails deploy-contract validation."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class FetchError(Exception):
    """Raised when docker-compose.yml cannot be fetched from the git URL."""
