"""Execution backends package.

Re-exports all backend classes and helpers for backward compatibility
with callers that import from ``lifecycle.backends``.
"""

from __future__ import annotations

from ._util import PruneImagesResult, collect_protected_image_refs
from .base import ExecutionBackend
from .docker_cli import DockerBackend
from .docker_sdk import DockerSdkBackend
from .multi_host import MultiHostBackend
from .noop import NoopBackend

__all__ = [
    "DockerBackend",
    "DockerSdkBackend",
    "ExecutionBackend",
    "MultiHostBackend",
    "NoopBackend",
    "PruneImagesResult",
    "collect_protected_image_refs",
]
