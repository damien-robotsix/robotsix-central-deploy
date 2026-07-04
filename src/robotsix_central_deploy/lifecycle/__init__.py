"""Lifecycle control API — start, stop, restart, and status for managed services."""

from .models import (
    ServiceState,
    ServiceRecord,
    ServiceStatus,
    ServiceListItem,
    ErrorDetail,
    DeployHistoryEntry,
    DeployHistoryResponse,
    RollbackRequest,
)
from .backends import (
    ExecutionBackend,
    DockerBackend,
    DockerSdkBackend,
    NoopBackend,
    collect_protected_image_refs,
)
from .store import ServiceStore, InMemoryStore, FileStore
from .config import LifecycleConfig
from .app import app

__all__ = [
    "ServiceState",
    "ServiceRecord",
    "ServiceStatus",
    "ServiceListItem",
    "ErrorDetail",
    "DeployHistoryEntry",
    "DeployHistoryResponse",
    "RollbackRequest",
    "ExecutionBackend",
    "DockerBackend",
    "DockerSdkBackend",
    "NoopBackend",
    "collect_protected_image_refs",
    "ServiceStore",
    "InMemoryStore",
    "FileStore",
    "LifecycleConfig",
    "app",
]
