"""Pluggable execution backends for actually starting/stopping services.

Provides an abstract ``ExecutionBackend`` and a ``DockerBackend`` that
drives ``docker`` / ``docker-compose`` via subprocess.

.. deprecated::
    Import from ``lifecycle.backends`` instead; this module exists for
    backward compatibility and re-exports everything from the per-class
    modules under ``lifecycle.backends.*``.
"""

from __future__ import annotations

from .backends.base import ExecutionBackend
from .backends.docker_cli import DockerBackend
from .backends.docker_sdk import DockerSdkBackend
from .backends.noop import NoopBackend
from .backends._util import collect_protected_image_refs

# Backward-compat: the original backend.py also imported these model types
# at module level, so callers could do ``from .backend import ComponentInspect``.
from .models import (  # noqa: F401
    ComponentInspect,
    DeployOutcome,
    DockerDfStats,
    HealthStatus,
    RollbackOutcome,
    SelfInspect,
    ServiceRecord,
    ServiceState,
    VolumeStat,
)

__all__ = [
    "ExecutionBackend",
    "NoopBackend",
    "DockerBackend",
    "DockerSdkBackend",
    "collect_protected_image_refs",
    "ComponentInspect",
    "DeployOutcome",
    "DockerDfStats",
    "HealthStatus",
    "RollbackOutcome",
    "SelfInspect",
    "ServiceRecord",
    "ServiceState",
    "VolumeStat",
]
