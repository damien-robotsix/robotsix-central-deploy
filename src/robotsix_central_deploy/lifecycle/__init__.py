"""Lifecycle control API — start, stop, restart, and status for managed services."""

from .models import (
    ServiceState,
    ServiceRecord,
    ServiceStatus,
    ServiceListItem,
    ErrorDetail,
)
from .backend import ExecutionBackend, DockerBackend
from .store import ServiceStore, InMemoryStore, FileStore
from .config import LifecycleConfig
from .server import app

__all__ = [
    "ServiceState",
    "ServiceRecord",
    "ServiceStatus",
    "ServiceListItem",
    "ErrorDetail",
    "ExecutionBackend",
    "DockerBackend",
    "ServiceStore",
    "InMemoryStore",
    "FileStore",
    "LifecycleConfig",
    "app",
]
