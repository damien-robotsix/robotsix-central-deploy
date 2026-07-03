"""Execution backends package.

Re-exports all backend classes and helpers for backward compatibility
with callers that import from ``lifecycle.backends``.
"""

from __future__ import annotations

from .base import ExecutionBackend
from .docker_cli import DockerBackend
from .docker_sdk import DockerSdkBackend
from .noop import NoopBackend
from ._util import collect_protected_image_refs

__all__ = [
    "ExecutionBackend",
    "DockerBackend",
    "DockerSdkBackend",
    "NoopBackend",
    "collect_protected_image_refs",
]
