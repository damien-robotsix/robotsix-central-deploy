"""Shared RetryClient factory for robotsix-central-deploy.

Provides a context manager for short-lived clients and a wrapper
for long-lived (lifespan-managed) clients.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from robotsix_http import DEFAULT_CONFIG, RetryClient, RetryConfig


@asynccontextmanager
async def retry_client_context(
    timeout: float = 30.0,
    *,
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> AsyncIterator[RetryClient]:
    """Async context manager yielding a ``RetryClient`` backed by a fresh
    ``httpx.AsyncClient``.

    The underlying ``httpx.AsyncClient`` is closed on context exit.
    """
    async with httpx.AsyncClient(timeout=timeout, **kwargs) as raw:
        yield RetryClient(raw, config=config if config is not None else DEFAULT_CONFIG)


def wrap_retry_client(
    raw: httpx.AsyncClient,
    *,
    config: RetryConfig | None = None,
) -> RetryClient:
    """Wrap an existing ``httpx.AsyncClient`` in a ``RetryClient``.

    The caller is responsible for closing *raw*.
    """
    return RetryClient(raw, config=config if config is not None else DEFAULT_CONFIG)
