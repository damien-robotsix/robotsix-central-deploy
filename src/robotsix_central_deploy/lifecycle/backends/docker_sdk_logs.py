"""Container log streaming for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends.docker_sdk.DockerSdkBackend`,
which provides ``_container_name`` and ``_get_container``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..models import ServiceRecord

logger = logging.getLogger(__name__)


class LogOpsMixin:
    """Log streaming / snapshot for :class:`DockerSdkBackend`."""

    if TYPE_CHECKING:

        def _container_name(self, service: ServiceRecord) -> str: ...

        async def _get_container(self, name: str) -> Any: ...

    async def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        """Stream container logs. Returns an async iterator of text chunks."""
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            yield f"[docker error: {exc}]\n".encode()
            return

        if container is None:
            yield b"[container not found]\n"
            return

        kwargs: dict[str, object] = {"stream": True, "follow": follow, "tail": tail}
        if since is not None:
            kwargs["since"] = since

        log_iter = None
        try:
            log_iter = await loop.run_in_executor(
                None, lambda: container.logs(**kwargs)
            )
            while True:

                def _next_chunk() -> tuple[bytes | None, bool]:
                    try:
                        return next(log_iter), False
                    except StopIteration:
                        return None, True

                chunk, exhausted = await loop.run_in_executor(None, _next_chunk)
                if exhausted:
                    break
                yield (
                    chunk
                    if isinstance(chunk, bytes)
                    else (chunk.encode() if chunk is not None else b"")
                )
        except asyncio.CancelledError:
            raise
        except docker.errors.APIError as exc:
            yield f"[docker error: {exc}]\n".encode()
        finally:
            if log_iter is not None:
                try:
                    log_iter.close()
                except (
                    Exception  # noqa: BLE001
                ):  # Best-effort close; iterator may already be exhausted
                    pass

    async def get_container_logs(
        self,
        service: ServiceRecord,
        tail: int = 200,
    ) -> str:
        """Return the last *tail* lines of a container's logs as a string.

        Returns an empty string if the container is not found or an error
        occurs.
        """
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)

        try:
            container = await self._get_container(name)
        except docker.errors.APIError:
            return ""

        if container is None:
            return ""

        try:
            raw = await loop.run_in_executor(
                None,
                lambda: container.logs(stdout=True, stderr=True, tail=tail),
            )
            return raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            logger.warning(
                "get_container_logs: failed to read logs for %s", name, exc_info=True
            )
            return ""
