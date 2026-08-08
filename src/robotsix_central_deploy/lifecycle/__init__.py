"""Lifecycle control API — start, stop, restart, and status for managed services."""

from .backends import (
    DockerBackend,
    DockerSdkBackend,
    ExecutionBackend,
    NoopBackend,
    collect_protected_image_refs,
)
from .config import LifecycleConfig
from .models import (
    DeployHistoryEntry,
    DeployHistoryResponse,
    ErrorDetail,
    RollbackRequest,
    ServiceListItem,
    ServiceRecord,
    ServiceState,
    ServiceStatus,
)
from .store import FileStore, InMemoryStore, ServiceStore

__all__ = [
    "DeployHistoryEntry",
    "DeployHistoryResponse",
    "DockerBackend",
    "DockerSdkBackend",
    "ErrorDetail",
    "ExecutionBackend",
    "FileStore",
    "InMemoryStore",
    "LifecycleConfig",
    "NoopBackend",
    "RollbackRequest",
    "ServiceListItem",
    "ServiceRecord",
    "ServiceState",
    "ServiceStatus",
    "ServiceStore",
    "collect_protected_image_refs",
]
