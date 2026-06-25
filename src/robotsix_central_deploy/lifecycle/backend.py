"""Pluggable execution backends for actually starting/stopping services.

Provides an abstract ``ExecutionBackend`` and a ``DockerBackend`` that
drives ``docker`` / ``docker-compose`` via subprocess.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

from .models import ServiceRecord, ServiceState

logger = logging.getLogger(__name__)


class ExecutionBackend(ABC):
    """Abstract interface for service lifecycle operations."""

    @abstractmethod
    async def start(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def stop(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def restart(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def status(self, service: ServiceRecord) -> ServiceState: ...


# ---------------------------------------------------------------------------
# Noop backend (for testing / dry runs)
# ---------------------------------------------------------------------------


class NoopBackend(ExecutionBackend):
    """Backend that does nothing — always reports success."""

    async def start(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.STOPPED

    async def restart(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.RUNNING

    async def status(self, service: ServiceRecord) -> ServiceState:
        return service.state


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


class DockerBackend(ExecutionBackend):
    """Executes lifecycle actions via the local Docker daemon (``docker`` CLI)."""

    async def start(self, service: ServiceRecord) -> ServiceState:
        if not service.image:
            logger.warning("Service %r has no image — cannot start via Docker", service.name)
            return ServiceState.FAILED

        # Try `docker start` (container may already exist), fall back to `docker run`.
        rc, _, stderr = await _run(
            "docker", "start", service.name,
        )
        if rc == 0:
            return ServiceState.RUNNING

        # Container may not exist — create and start it.
        rc, _, stderr = await _run(
            "docker", "run", "-d", "--name", service.name, service.image,
        )
        if rc != 0:
            logger.error("docker run %s failed: %s", service.name, stderr)
            return ServiceState.FAILED
        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        rc, _, stderr = await _run("docker", "stop", service.name)
        if rc != 0:
            # If it's already stopped, treat as success.
            state = await self._inspect_state(service.name)
            if state in (ServiceState.STOPPED, None):
                return ServiceState.STOPPED
            logger.error("docker stop %s failed: %s", service.name, stderr)
            return ServiceState.FAILED
        return ServiceState.STOPPED

    async def restart(self, service: ServiceRecord) -> ServiceState:
        if not service.image:
            # Restart requires we can recreate; treat like stop+start.
            stop_state = await self.stop(service)
            if stop_state == ServiceState.FAILED:
                return ServiceState.FAILED
            return await self.start(service)

        rc, _, stderr = await _run("docker", "restart", service.name)
        if rc != 0:
            # Container may not exist — fall back to stop + start.
            await self.stop(service)
            return await self.start(service)
        return ServiceState.RUNNING

    async def status(self, service: ServiceRecord) -> ServiceState:
        return await self._inspect_state(service.name) or ServiceState.UNKNOWN

    async def _inspect_state(self, container_name: str) -> Optional[ServiceState]:
        """Map ``docker inspect`` output to a ``ServiceState``."""
        rc, stdout, _stderr = await _run(
            "docker", "inspect", "-f", "{{.State.Status}}", container_name,
        )
        if rc != 0:
            return None  # Container not found → treat as unknown.

        status = stdout.strip().lower()
        mapping: dict[str, ServiceState] = {
            "running": ServiceState.RUNNING,
            "paused": ServiceState.RUNNING,
            "restarting": ServiceState.RESTARTING,
            "created": ServiceState.STOPPED,
            "exited": ServiceState.STOPPED,
            "dead": ServiceState.FAILED,
            "removing": ServiceState.STOPPING,
        }
        return mapping.get(status, ServiceState.UNKNOWN)


# ---------------------------------------------------------------------------
# subprocess helper
# ---------------------------------------------------------------------------


async def _run(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        logger.warning("Command timed out after %.1fs: %s", timeout, args)
        return (-1, "", f"timed out after {timeout}s")
    except FileNotFoundError:
        logger.error("Command not found: %s", args[0])
        return (-1, "", f"executable not found: {args[0]}")
    except Exception as exc:
        logger.exception("Unexpected error running %s", args)
        return (-1, "", str(exc))
