"""Docker SDK backend — executes lifecycle actions via the Docker Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

from .base import ExecutionBackend
from ...gateway.proxy import PROXY_NETWORK
from robotsix_central_deploy._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
)
from ..models import (
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

if TYPE_CHECKING:
    from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)

CLAUDE_AUTH_VOLUME = "claude-auth"
CLAUDE_AUTH_HOST_SEED = "/home/debian/.claude"


class DockerSdkBackend(ExecutionBackend):
    """Executes lifecycle actions via the Docker Python SDK against the local socket."""

    def __init__(
        self,
        socket_url: str = "unix:///var/run/docker.sock",
        timeout: int = 120,
    ) -> None:
        import docker

        self._client = docker.DockerClient(base_url=socket_url, timeout=timeout)

    # -- helpers ------------------------------------------------------------

    def _container_name(self, service: ServiceRecord) -> str:
        return service.container_name if service.container_name else service.name

    @staticmethod
    def _state_from_docker(status: str) -> ServiceState:
        status = status.lower()
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

    async def _get_container(self, name: str) -> Any:
        """Run ``containers.get`` in the default executor and map known errors."""
        import docker

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                self._client.containers.get,
                name,
            )
        except docker.errors.NotFound:
            return None
        except docker.errors.APIError:
            raise

    # -- ExecutionBackend ---------------------------------------------------

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during status(%s): %s", name, exc)
            return ComponentInspect(state=ServiceState.UNKNOWN)

        if container is None:
            logger.warning("Container %s not found during status", name)
            return ComponentInspect(state=ServiceState.UNKNOWN)

        loop = asyncio.get_running_loop()

        def _inspect() -> ComponentInspect:
            state_str = container.attrs["State"]["Status"]
            state = self._state_from_docker(state_str)

            # image revision label from the container's image
            revision = ""
            try:
                revision = container.image.labels.get(
                    "org.opencontainers.image.revision",
                    "",
                )
            except Exception:  # Gracefully degrade; label stays empty
                pass

            # health check result
            health = ""
            try:
                health_obj = container.attrs["State"].get("Health")
                if health_obj:
                    health = health_obj.get("Status", "")
            except Exception:  # Gracefully degrade; health stays empty
                pass

            # running_digest from the image's RepoDigests
            running_digest = ""
            try:
                image_id = container.attrs.get("Image", "")
                if image_id:
                    img = self._client.images.get(image_id)
                    repo_digests = img.attrs.get("RepoDigests", [])
                    # Prefer an entry matching the service image (strips tag)
                    prefix = service.image.rsplit(":", 1)[0] + "@"
                    for rd in repo_digests:
                        if rd.startswith(prefix):
                            running_digest = rd.split("@", 1)[1]
                            break
                    if not running_digest:
                        # Fallback: any RepoDigest entry with sha256
                        for rd in repo_digests:
                            if "@sha256:" in rd:
                                running_digest = rd.split("@", 1)[1]
                                break
            except Exception:
                pass  # Gracefully degrade; digest stays ""

            return ComponentInspect(
                state=state,
                image_revision=revision,
                health=health,
                running_digest=running_digest,
            )

        return await loop.run_in_executor(None, _inspect)

    async def start(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during start(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.warning("Container %s not found — deploy first", name)
            return ServiceState.FAILED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.start)
        except docker.errors.APIError as exc:
            logger.error("Docker API error starting %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during stop(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.debug("Container %s not found — already stopped", name)
            return ServiceState.STOPPED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.stop)
        except docker.errors.APIError as exc:
            logger.error("Docker API error stopping %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.STOPPED

    async def remove_container(self, service: ServiceRecord) -> None:
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)
        container = await self._get_container(name)
        if container is None:
            return
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except docker.errors.NotFound:  # Container already removed
            pass
        except Exception as exc:
            logger.warning("remove_container %s: %s", name, exc)

    async def restart(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during restart(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.warning("Container %s not found — deploy first", name)
            return ServiceState.FAILED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.restart)
        except docker.errors.APIError as exc:
            logger.error("Docker API error restarting %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.RUNNING

    # -- deploy / rollback --------------------------------------------------

    def _create_container(self, config: "ComponentConfig", image_ref: str) -> Any:
        """Create a Docker container from a ComponentConfig spec (synchronous)."""

        # Host ports are intentionally NOT published: the gateway reaches
        # managed containers over the central-deploy-proxy network by
        # container_name:container_port (gateway/router.py). Publishing host
        # ports caused "port is already allocated" conflicts with existing
        # host-bound services.
        ports: dict[str, Any] = {}
        volumes = {
            m.host: {"bind": m.container, "mode": "ro" if m.read_only else "rw"}
            for m in config.mounts
        }
        if config.claude_mount:
            volumes[CLAUDE_AUTH_VOLUME] = {
                "bind": "/home/app/.claude",
                "mode": "rw",
            }
        if config.host_docker_sock:
            volumes["/var/run/docker.sock"] = {
                "bind": "/var/run/docker.sock",
                "mode": "ro",
            }
        healthcheck = None
        if config.health_check:
            hc = config.health_check
            healthcheck = {
                "Test": hc.test,
                "Interval": hc.interval_seconds * int(1e9),
                "Timeout": hc.timeout_seconds * int(1e9),
                "Retries": hc.retries,
                "StartPeriod": hc.start_period_seconds * int(1e9),
            }
        return self._client.containers.create(
            image=image_ref,
            name=config.container_name,
            command=config.command,
            entrypoint=config.entrypoint,
            environment=config.env,
            volumes=volumes,
            healthcheck=healthcheck,
            ports=ports,
            detach=True,
            user=f"{os.getuid()}:{os.getgid()}",
            restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]  # types-docker stubs are incomplete for restart policy names
            network=PROXY_NETWORK,
        )

    async def _wait_healthy(self, name: str, timeout: float = 60.0) -> None:
        """Poll container health status until healthy, or raise on unhealthy/timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            container = await self._get_container(name)
            if container is None:
                raise RuntimeError(f"Container {name} disappeared during health wait")

            def _poll() -> str:
                container.reload()
                h = container.attrs["State"].get("Health")
                return (
                    h["Status"] if h else HealthStatus.HEALTHY
                )  # no healthcheck → treat as healthy

            status = await loop.run_in_executor(None, _poll)
            if status == HealthStatus.HEALTHY:
                return
            if status == HealthStatus.UNHEALTHY:
                raise RuntimeError(f"Container {name} is unhealthy after deploy")
            await asyncio.sleep(2)
        logger.warning(
            "Health wait timed out for %s after %.0fs — proceeding", name, timeout
        )

    def _stop_and_remove(self, container: Any) -> None:
        """Stop and force-remove a container (synchronous, best-effort stop)."""
        try:
            container.stop(timeout=30)
        except Exception:  # Best-effort stop; proceed to force-remove
            pass
        container.remove(force=True)

    # -- claude-auth volume helpers -----------------------------------------

    def _ensure_claude_auth_volume(self) -> None:
        """Create the ``claude-auth`` named volume if it does not exist.

        Labels it with ``robotsix.deploy.stateful=true`` for consistency
        with other managed stateful volumes.
        """
        import docker

        try:
            self._client.volumes.get(CLAUDE_AUTH_VOLUME)
        except docker.errors.NotFound:
            self._client.volumes.create(
                CLAUDE_AUTH_VOLUME,
                labels={"robotsix.deploy.stateful": "true"},
            )

    def _seed_claude_auth_from_host(self) -> bool:
        """Seed the ``claude-auth`` volume from ``/home/debian/.claude``.

        Copies contents with uid:gid 1000:1000 and perms 0700/0600, then
        removes the host source directory.  Idempotent: returns ``True``
        only the first time it actually copies data; returns ``False`` if
        the volume is already seeded or the host source is absent.
        """
        import docker

        host_src = CLAUDE_AUTH_HOST_SEED
        if not os.path.isdir(host_src):
            return False

        # Check whether the volume already has credentials.
        try:
            self._client.containers.run(
                "busybox",
                command=[
                    "sh",
                    "-c",
                    "test -f /mnt/.credentials.json",
                ],
                volumes={CLAUDE_AUTH_VOLUME: {"bind": "/mnt", "mode": "ro"}},
                remove=True,
            )
            # Exit 0 → .credentials.json exists → already seeded.
            return False
        except docker.errors.ContainerError:
            # Exit non-zero → .credentials.json missing → seed.
            pass

        logger.info(
            "Seeding %s volume from %s (chown 1000:1000) ...",
            CLAUDE_AUTH_VOLUME,
            host_src,
        )

        # Copy host source into the volume using a privileged helper.
        # busybox cp -a copies recursively; we then chown everything.
        script = (
            "cp -a /host/. /mnt/ && "
            "chown -R 1000:1000 /mnt && "
            "find /mnt -type d -exec chmod 700 {} + && "
            "find /mnt -type f -exec chmod 600 {} +"
        )
        try:
            self._client.containers.run(
                "busybox",
                command=["sh", "-c", script],
                volumes={
                    host_src: {"bind": "/host", "mode": "ro"},
                    CLAUDE_AUTH_VOLUME: {"bind": "/mnt", "mode": "rw"},
                },
                remove=True,
            )
        except docker.errors.ContainerError as exc:
            logger.error("Failed to seed %s volume: %s", CLAUDE_AUTH_VOLUME, exc)
            return False

        # Remove the host source directory.
        try:
            shutil.rmtree(host_src)
        except OSError as exc:
            logger.warning("Could not remove host seed dir %s: %s", host_src, exc)

        logger.info("Successfully seeded %s volume.", CLAUDE_AUTH_VOLUME)
        return True

    def _check_claude_credentials(self) -> list[str]:
        """Validate that the ``claude-auth`` volume contains a readable
        ``.credentials.json``. Returns a list of warning strings (empty
        if credentials are valid).
        """
        import docker

        warnings: list[str] = []
        try:
            self._client.volumes.get(CLAUDE_AUTH_VOLUME)
        except docker.errors.NotFound:
            return [
                f"Claude auth volume '{CLAUDE_AUTH_VOLUME}' does not exist. "
                f"Your component requests a Claude mount but no credentials "
                f"are available. Use the dashboard 'Claude auth' panel to "
                f"provision credentials, then redeploy."
            ]

        # Check if .credentials.json exists and is a regular file.
        try:
            self._client.containers.run(
                "busybox",
                command=[
                    "sh",
                    "-c",
                    "test -f /mnt/.credentials.json && test -r /mnt/.credentials.json",
                ],
                volumes={CLAUDE_AUTH_VOLUME: {"bind": "/mnt", "mode": "ro"}},
                remove=True,
            )
        except docker.errors.ContainerError:
            warnings.append(
                f"Claude auth volume '{CLAUDE_AUTH_VOLUME}' exists but does not "
                f"contain a readable .credentials.json. Your component requests "
                f"a Claude mount but has no valid credentials. Use the dashboard "
                f"'Claude auth' panel to provision credentials, then redeploy."
            )

        return warnings

    # -- claude-auth API (dashboard panel) ----------------------------------

    async def check_claude_auth(self, volume_name: str) -> dict[str, Any]:
        """Check whether *volume_name* holds valid Claude credentials."""
        import docker
        import json as _json

        loop = asyncio.get_running_loop()

        def _check() -> dict[str, Any]:
            # Ensure the volume exists.
            try:
                self._client.volumes.get(volume_name)
            except docker.errors.NotFound:
                return {
                    "status": "not-authenticated",
                    "detail": f"Volume '{volume_name}' does not exist.",
                }

            # Check for .credentials.json existence and parse it.
            try:
                result = self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "cat /mnt/.credentials.json 2>/dev/null || echo 'MISSING'",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                    remove=True,
                )
                content = result.decode("utf-8", errors="replace").strip()
            except docker.errors.ContainerError:
                return {
                    "status": "not-authenticated",
                    "detail": "Failed to read credentials from volume.",
                }

            if content == "MISSING" or not content:
                return {
                    "status": "not-authenticated",
                    "detail": "No credentials file found.",
                }

            try:
                creds = _json.loads(content)
            except _json.JSONDecodeError:
                return {
                    "status": "error",
                    "detail": "Credentials file exists but is not valid JSON.",
                }

            # Check for expiry information (typical in OAuth credentials).
            # Anthropic stores expiration as an ISO timestamp in the credentials.
            expires_at = creds.get("expires_at") or creds.get("expiresAt")
            if expires_at:
                try:
                    from datetime import datetime, timezone

                    if isinstance(expires_at, str):
                        expire_dt = datetime.fromisoformat(
                            expires_at.replace("Z", "+00:00")
                        )
                        now = datetime.now(timezone.utc)
                        if expire_dt < now:
                            return {
                                "status": "not-authenticated",
                                "detail": "Credentials have expired.",
                            }
                        remaining = (expire_dt - now).total_seconds()
                        if remaining < 86400:  # less than 1 day
                            return {
                                "status": "expiring",
                                "detail": f"Credentials expire in {remaining / 3600:.1f} hours.",
                            }
                except ValueError, TypeError:
                    pass  # unparseable expiry → treat as valid

            return {"status": "authenticated"}

        return await loop.run_in_executor(None, _check)

    async def start_claude_login(
        self, volume_name: str, helper_image: str
    ) -> dict[str, Any]:
        """Spawn a helper container that runs ``claude login``, returning the
        OAuth URL for the operator to visit.

        The helper script:
        1. Runs ``claude login``, capturing stdout/stderr to a volume file.
        2. Writes the OAuth URL to ``.login-url`` on the volume.
        3. Waits for either an auth code (written to ``.login-code``) or for
           ``claude login`` to complete on its own (device-flow OAuth callback).
        4. On completion writes the exit code to ``.login-result``.
        """
        import docker

        loop = asyncio.get_running_loop()

        # The helper script captures the OAuth URL from claude login output,
        # then waits for the operator to either paste a code or authorize via
        # the OAuth device flow (which completes automatically).
        script = (
            "claude login > /mnt/.login-output 2>&1 & "
            "CLAUDE_PID=$!; "
            # Poll for the OAuth URL to appear in the output.
            "for i in $(seq 1 60); do "
            "  if grep -qE 'https?://' /mnt/.login-output 2>/dev/null; then "
            "    grep -oE 'https?://[^[:space:]]+' /mnt/.login-output | head -1 > /mnt/.login-url; "
            "    touch /mnt/.login-ready; "
            "    break; "
            "  fi; "
            "  sleep 1; "
            "done; "
            # Wait for either a pasted code or for claude login to finish.
            "for i in $(seq 1 300); do "
            "  if [ -f /mnt/.login-code ]; then "
            "    CODE=$(cat /mnt/.login-code); rm -f /mnt/.login-code; "
            '    echo "$CODE" | claude login 2>&1; '
            '    RC=$?; echo "EXIT:$RC" > /mnt/.login-result; '
            "    kill $CLAUDE_PID 2>/dev/null; wait $CLAUDE_PID 2>/dev/null; "
            "    break; "
            "  fi; "
            "  if ! kill -0 $CLAUDE_PID 2>/dev/null; then "
            "    wait $CLAUDE_PID 2>/dev/null; "
            '    echo "EXIT:$?" > /mnt/.login-result; '
            "    break; "
            "  fi; "
            "  sleep 1; "
            "done; "
            # Timeout guard.
            "if ! [ -f /mnt/.login-result ]; then "
            "  kill $CLAUDE_PID 2>/dev/null; wait $CLAUDE_PID 2>/dev/null; "
            "  echo 'EXIT:124' > /mnt/.login-result; "
            "fi; "
            "rm -f /mnt/.login-ready /mnt/.login-url"
        )

        container_name = f"claude-login-{os.urandom(4).hex()}"

        def _start() -> dict[str, Any]:
            container = self._client.containers.create(
                helper_image,
                command=["sh", "-c", script],
                volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                name=container_name,
                user="1000:1000",
                detach=True,
            )
            container.start()

            # Poll for the OAuth URL to appear in the volume.
            import time

            deadline = time.monotonic() + 120
            oauth_url = ""
            while time.monotonic() < deadline:
                try:
                    result = self._client.containers.run(
                        "busybox",
                        command=[
                            "sh",
                            "-c",
                            "cat /mnt/.login-ready 2>/dev/null || true",
                        ],
                        volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                        remove=True,
                    )
                    if result.strip():
                        url_result = self._client.containers.run(
                            "busybox",
                            command=[
                                "sh",
                                "-c",
                                "cat /mnt/.login-url 2>/dev/null || true",
                            ],
                            volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                            remove=True,
                        )
                        oauth_url = url_result.decode("utf-8", errors="replace").strip()
                        break
                except docker.errors.ContainerError:
                    pass
                time.sleep(1)

            if not oauth_url:
                # Check if the container exited already (error).
                try:
                    container.reload()
                    if container.status in ("exited", "dead"):
                        logs = container.logs(stdout=True, stderr=True).decode(
                            errors="replace"
                        )
                        if not logs.strip():
                            try:
                                file_result = self._client.containers.run(
                                    "busybox",
                                    command=[
                                        "sh",
                                        "-c",
                                        "cat /mnt/.login-output 2>/dev/null || true",
                                    ],
                                    volumes={
                                        volume_name: {"bind": "/mnt", "mode": "ro"}
                                    },
                                    remove=True,
                                )
                                logs = file_result.decode("utf-8", errors="replace")
                            except Exception:
                                pass
                        try:
                            container.remove(force=True)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Claude login helper exited prematurely:\n{logs}"
                        )
                except docker.errors.NotFound:
                    pass
                try:
                    container.kill()
                    container.remove(force=True)
                except Exception:
                    pass
                raise RuntimeError(
                    "Claude login: could not obtain OAuth URL within 120s."
                )

            return {"container_id": container.id, "oauth_url": oauth_url}

        try:
            return await loop.run_in_executor(None, _start)
        except Exception:
            # Best-effort cleanup on failure.
            try:
                c = self._client.containers.get(container_name)
                c.kill()
                c.remove(force=True)
            except Exception:
                pass
            raise

    async def complete_claude_login(
        self, volume_name: str, container_id: str, auth_code: str
    ) -> dict[str, Any]:
        """Feed *auth_code* to the waiting helper container (if needed) and
        wait for it to complete.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _complete() -> dict[str, Any]:
            # If an auth code is provided, write it to the volume for the
            # helper to pick up.  The helper also handles the case where
            # claude login completes via device-flow OAuth without a code.
            if auth_code.strip():
                self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "printf '%s' \"$1\" > /mnt/.login-code",
                        "_",
                        auth_code.strip(),
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                    user="1000:1000",
                    remove=True,
                )

            # Wait for the helper container to finish.
            import time

            try:
                container = self._client.containers.get(container_id)
            except docker.errors.NotFound:
                return {"status": "error", "error": "Helper container not found."}

            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                container.reload()
                if container.status in ("exited", "dead"):
                    break
                time.sleep(1)
            else:
                try:
                    container.kill()
                    container.remove(force=True)
                except Exception:
                    pass
                return {"status": "error", "error": "Login timed out after 300s."}

            # Read the result.
            try:
                result = self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "cat /mnt/.login-result 2>/dev/null || echo 'EXIT:1'",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                    remove=True,
                )
                result_str = result.decode("utf-8", errors="replace").strip()
            except docker.errors.ContainerError:
                result_str = "EXIT:1"

            logs = ""
            try:
                logs = container.logs(stdout=True, stderr=True).decode(errors="replace")
            except Exception:
                pass
            if not logs.strip():
                try:
                    file_result = self._client.containers.run(
                        "busybox",
                        command=[
                            "sh",
                            "-c",
                            "cat /mnt/.login-output 2>/dev/null || true",
                        ],
                        volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                        remove=True,
                    )
                    logs = file_result.decode("utf-8", errors="replace")
                except Exception:
                    pass

            # Cleanup the container.
            try:
                container.remove(force=True)
            except Exception:
                pass

            # Cleanup temp files.
            try:
                self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "rm -f /mnt/.login-result /mnt/.login-ready /mnt/.login-url /mnt/.login-code /mnt/.login-output",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                    remove=True,
                )
            except Exception:
                pass

            if result_str.startswith("EXIT:0"):
                return {"status": "authenticated"}
            else:
                return {
                    "status": "error",
                    "error": f"Claude login failed: {result_str}",
                    "logs": logs[-2000:],
                }

        return await loop.run_in_executor(None, _complete)

    async def cancel_claude_login(self, volume_name: str, container_id: str) -> None:
        """Kill and remove a running claude-login helper container."""
        import docker

        loop = asyncio.get_running_loop()

        def _cancel() -> None:
            try:
                container = self._client.containers.get(container_id)
                container.kill()
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            # Clean up temp files
            try:
                self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "rm -f /mnt/.login-ready /mnt/.login-url /mnt/.login-code /mnt/.login-result /mnt/.login-output",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                    remove=True,
                )
            except Exception:
                pass

        await loop.run_in_executor(None, _cancel)

    async def write_claude_credentials(
        self, volume_name: str, credentials_json: str
    ) -> dict[str, Any]:
        """Write *credentials_json* into *volume_name* as ``.credentials.json``."""
        import docker
        import json as _json

        # Validate that it's at least parseable JSON.
        try:
            _json.loads(credentials_json)
        except _json.JSONDecodeError as exc:
            return {"status": "error", "error": f"Invalid JSON: {exc}"}

        loop = asyncio.get_running_loop()

        def _write() -> dict[str, Any]:
            # Ensure the volume exists.
            try:
                self._client.volumes.get(volume_name)
            except docker.errors.NotFound:
                return {
                    "status": "error",
                    "error": f"Volume '{volume_name}' does not exist.",
                }

            encoded = credentials_json.encode("utf-8")
            import base64

            b64 = base64.b64encode(encoded).decode("ascii")

            self._client.containers.run(
                "busybox",
                command=[
                    "sh",
                    "-c",
                    'echo "$B64" | base64 -d > /mnt/.credentials.json && chown 1000:1000 /mnt/.credentials.json && chmod 600 /mnt/.credentials.json',
                ],
                environment={"B64": b64},
                volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                remove=True,
            )
            return {"status": "authenticated"}

        return await loop.run_in_executor(None, _write)

    async def deploy(
        self, service: ServiceRecord, config: "ComponentConfig", image_ref: str
    ) -> DeployOutcome:
        """Pull *image_ref*, recreate the container from *config*, return outcome."""
        import docker

        name = self._container_name(service)
        loop = asyncio.get_running_loop()

        # Step 1 — pull target image; obtain its digest
        try:
            image = await loop.run_in_executor(
                None, lambda: self._client.images.pull(image_ref)
            )
        except docker.errors.APIError as exc:
            raise RuntimeError(f"Image pull failed for {image_ref!r}: {exc}") from exc
        # Derive manifest digest from RepoDigests (comparable to registry
        # Docker-Content-Digest header), falling back to config digest.
        # Strip a digest suffix first (repo@sha256:… — the caretaker deploys
        # pinned refs), then the tag, so RepoDigests matching works for both.
        repo_without_tag = image_ref.split("@", 1)[0].rsplit(":", 1)[0]
        repo_digests = image.attrs.get("RepoDigests", [])
        new_digest: str = next(
            (
                rd.split("@")[1]
                for rd in repo_digests
                if rd.startswith(repo_without_tag + "@")
            ),
            image.id or "",
        )

        # Step 2 — snapshot current container's image digest (for rollback)
        prior_digest = ""
        existing = await self._get_container(name)
        if existing is not None:
            try:
                prior_digest = await loop.run_in_executor(
                    None, lambda: existing.image.id
                )
            except Exception:  # Gracefully degrade; prior_digest stays empty
                pass

        # Step 3 — stop + remove old container (if present)
        if existing is not None:
            try:
                await loop.run_in_executor(
                    None, lambda: self._stop_and_remove(existing)
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to remove existing container {name!r}: {exc}"
                ) from exc

        # Step 4 — create + start new container
        deploy_warnings: list[str] = []
        try:
            # Pre-create named volumes
            for vol_name in config.named_volumes:
                try:
                    await loop.run_in_executor(
                        None, self._client.volumes.create, vol_name
                    )
                except docker.errors.APIError as exc:
                    if exc.status_code == 409:
                        logger.info(
                            "Volume %s already exists, skipping creation", vol_name
                        )
                    else:
                        raise RuntimeError(
                            f"Failed to create volume {vol_name!r}: {exc.explanation or exc}"
                        ) from exc
                except docker.errors.DockerException as exc:
                    raise RuntimeError(
                        f"Docker daemon unreachable while creating volume {vol_name!r}: {exc}"
                    ) from exc

            # Claude auth: ensure named volume, seed from host if needed, validate
            if config.claude_mount:
                try:
                    await loop.run_in_executor(None, self._ensure_claude_auth_volume)
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to ensure claude-auth volume: {exc}"
                    ) from exc

                # Seed on first use (migration from host path)
                try:
                    await loop.run_in_executor(None, self._seed_claude_auth_from_host)
                except Exception as exc:
                    logger.warning(
                        "claude-auth seed migration failed (non-fatal): %s", exc
                    )

                # Validate credentials
                try:
                    cred_warnings = await loop.run_in_executor(
                        None, self._check_claude_credentials
                    )
                    if cred_warnings:
                        deploy_warnings.extend(cred_warnings)
                        for w in cred_warnings:
                            logger.warning(w)
                except Exception as exc:
                    logger.warning(
                        "claude-auth credential check failed (non-fatal): %s", exc
                    )

            new_container = await loop.run_in_executor(
                None, lambda: self._create_container(config, image_ref)
            )
            await loop.run_in_executor(None, new_container.start)
        except Exception as exc:
            # Best-effort restore: if we have a prior digest, try to recreate it
            if prior_digest:
                logger.error(
                    "deploy %s failed after container removal — attempting restore from %s",
                    name,
                    prior_digest,
                )
                try:
                    restore = await loop.run_in_executor(
                        None, lambda: self._create_container(config, prior_digest)
                    )
                    await loop.run_in_executor(None, restore.start)
                    logger.info("Restored %s from prior digest %s", name, prior_digest)
                except Exception as restore_exc:
                    logger.error("Restore of %s also failed: %s", name, restore_exc)
            raise RuntimeError(
                f"Container create/start failed for {name!r}: {exc}"
            ) from exc

        # Step 5 — health wait (if configured)
        if config.health_check:
            await self._wait_healthy(name, timeout=60.0)

        return DeployOutcome(
            deployed_digest=new_digest,
            previous_digest=prior_digest,
            state=ServiceState.RUNNING,
            warnings=deploy_warnings,
        )

    async def rollback(
        self, service: ServiceRecord, config: "ComponentConfig"
    ) -> RollbackOutcome:
        """Recreate container from ``service.previous_image_digest``."""
        import docker

        name = self._container_name(service)
        target_digest = (
            service.previous_image_digest
        )  # guaranteed non-empty by server layer
        loop = asyncio.get_running_loop()

        rollback_warnings: list[str] = []

        # Stop + remove current container
        existing = await self._get_container(name)
        if existing is not None:
            try:
                await loop.run_in_executor(
                    None, lambda: self._stop_and_remove(existing)
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to remove container {name!r} for rollback: {exc}"
                ) from exc

        # Create + start from prior digest
        try:
            # Claude auth: ensure named volume, seed from host if needed, validate
            if config.claude_mount:
                try:
                    await loop.run_in_executor(None, self._ensure_claude_auth_volume)
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to ensure claude-auth volume during rollback: {exc}"
                    ) from exc

                # Seed on first use (migration from host path)
                try:
                    await loop.run_in_executor(None, self._seed_claude_auth_from_host)
                except Exception as exc:
                    logger.warning(
                        "claude-auth seed migration failed during rollback (non-fatal): %s",
                        exc,
                    )

                # Validate credentials
                try:
                    cred_warnings = await loop.run_in_executor(
                        None, self._check_claude_credentials
                    )
                    if cred_warnings:
                        rollback_warnings.extend(cred_warnings)
                        for w in cred_warnings:
                            logger.warning(w)
                except Exception as exc:
                    logger.warning(
                        "claude-auth credential check failed during rollback (non-fatal): %s",
                        exc,
                    )

            rollback_container = await loop.run_in_executor(
                None, lambda: self._create_container(config, target_digest)
            )
            await loop.run_in_executor(None, rollback_container.start)
        except Exception as exc:
            raise RuntimeError(
                f"Rollback container create/start failed for {name!r}: {exc}"
            ) from exc

        if config.health_check:
            await self._wait_healthy(name, timeout=60.0)

        return RollbackOutcome(
            deployed_digest=target_digest,
            state=ServiceState.RUNNING,
            warnings=rollback_warnings,
        )

    # -- config volume helpers ----------------------------------------------

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as JSON into a Docker named volume via a
        temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64
        import json

        import docker

        json_content = json.dumps(config_dict, indent=2, sort_keys=True)
        encoded = base64.b64encode(json_content.encode()).decode()
        # base64 output contains only [A-Za-z0-9+/=] — safe to interpolate in sh without quoting
        cmd = f"mkdir -p /config && echo {encoded} | base64 -d > /config/config.json && chmod 777 /config && chmod 666 /config/config.json"
        loop = asyncio.get_running_loop()

        def _run() -> None:
            try:
                self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", cmd],
                    volumes={volume_name: {"bind": "/config", "mode": "rw"}},
                    remove=True,
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"write_config_to_volume failed for {volume_name}: {exc}"
                ) from exc

        await loop.run_in_executor(None, _run)

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.json from a named volume via a temporary busybox container."""
        import json

        loop = asyncio.get_running_loop()

        def _run() -> dict[str, Any]:
            import docker

            try:
                raw = self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", "cat /config/config.json 2>/dev/null || true"],
                    volumes={volume_name: {"bind": "/config", "mode": "ro"}},
                    remove=True,
                )
                text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw

                if not text.strip():
                    return {}
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise InvalidConfigStructureError(
                        f"Expected a mapping in Docker volume {volume_name}, "
                        f"got {type(data).__name__}"
                    )
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                raise YamlParseError(
                    f"JSON parse error in Docker volume {volume_name}: {exc}"
                ) from exc
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"read_config_from_volume failed for {volume_name}: {exc}"
                ) from exc

        return await loop.run_in_executor(None, _run)

    # -- volume inspection helpers ------------------------------------------

    async def measure_volume_bytes(self, volume_name: str) -> int:
        loop = asyncio.get_running_loop()
        cmd = (
            "find /vol -type f "
            "! -name '*.db-wal' ! -name '*.db-shm' ! -name '*.db-journal' "
            "-exec du -b {} + 2>/dev/null "
            "| awk '{s+=$1}END{print s+0}'"
        )
        try:
            raw: bytes = await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", cmd],
                    volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                    remove=True,
                ),
            )
            return int(raw.strip() or b"0")
        except Exception as exc:
            logger.warning("measure_volume_bytes(%r) failed: %s", volume_name, exc)
            return 0

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """List immediate children of /vol/<rel_path> via busybox.

        Uses ``find`` with ``-maxdepth 1`` for consistency.
        """
        loop = asyncio.get_running_loop()
        script = (
            'cd /vol && for f in "$1"/* "$1"/.*; do\n'
            '  [ -e "$f" ] || continue\n'
            '  bn="${f##*/}"\n'
            '  [ "$bn" = . ] && continue\n'
            '  [ "$bn" = .. ] && continue\n'
            '  if [ -d "$f" ]; then\n'
            '    printf "dir\\t0\\t%s\\n" "$bn"\n'
            "  else\n"
            '    sz=$(stat -c "%s" "$f" 2>/dev/null || echo 0)\n'
            '    printf "file\\t%s\\t%s\\n" "$sz" "$bn"\n'
            "  fi\n"
            "done\n"
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        entries: list[dict[str, Any]] = []
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            typ, size_str, name = parts
            try:
                size_bytes = int(size_str)
            except ValueError:
                size_bytes = 0
            entries.append({"name": name, "type": typ, "size_bytes": size_bytes})
        return entries

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        """Read ``/vol/<rel_path>`` via a one-shot busybox container.

        Returns size, content (or None for binary), binary flag, truncated flag.
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path, $2 = max_bytes+1 (head limit)
        script = (
            'target="/vol/$1"\n'
            "maxp1=$2\n"
            'stat -c "%s" "$target" 2>/dev/null || echo 0\n'
            'head -c "$maxp1" "$target" 2>/dev/null || true\n'
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path, str(max_bytes + 1)],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        # First line is the file size; the rest is the file content.
        lines = raw.split(b"\n", 1)
        try:
            size_bytes = int(lines[0].strip())
        except ValueError, IndexError:
            size_bytes = 0
        body = lines[1] if len(lines) > 1 else b""

        truncated = len(body) > max_bytes
        if truncated:
            body = body[:max_bytes]

        binary = b"\x00" in body
        content: str | None = None
        if not binary:
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                binary = True

        return {
            "size_bytes": size_bytes,
            "content": content,
            "binary": binary,
            "truncated": truncated,
        }

    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name* (best-effort).

        Swallows ``docker.errors.NotFound`` (already gone) and logs a
        warning on any other error — never raises, so a failed volume
        removal cannot abort a component delete.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _remove() -> None:
            self._client.volumes.get(volume_name).remove(force=True)

        try:
            await loop.run_in_executor(None, _remove)
        except docker.errors.NotFound:  # Volume already removed
            pass
        except Exception as exc:
            logger.warning("remove_volume %s: %s", volume_name, exc)

    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        """Run a one-shot container from *image*, mount config volume at *volume_mount_path*."""
        import requests.exceptions

        loop = asyncio.get_running_loop()

        def _run() -> str:
            container = self._client.containers.create(
                image,
                command=shlex.split(command_str),
                volumes={volume_name: {"bind": volume_mount_path, "mode": "rw"}},
                environment=env_dict,
            )
            try:
                container.start()
                result = container.wait(timeout=timeout_seconds)
                logs: str = container.logs(stdout=True, stderr=True).decode(
                    errors="replace"
                )
                exit_code = result.get("StatusCode", 0)
                if exit_code != 0:
                    raise RuntimeError(
                        f"config-assist exited with code {exit_code}:\n{logs}"
                    )
                return logs
            except requests.exceptions.ReadTimeout:
                try:
                    container.kill()
                except Exception:  # Best-effort kill; container may already be gone
                    pass
                raise TimeoutError(f"config-assist timed out after {timeout_seconds}s")
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        return await loop.run_in_executor(None, _run)

    async def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
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
                    Exception
                ):  # Best-effort close; iterator may already be exhausted
                    pass

    async def disk_df(self) -> DockerDfStats:
        import docker  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._client.api.df)
        except docker.errors.APIError as exc:
            logger.warning("docker df failed: %s", exc)
            return DockerDfStats()
        images = result.get("Images") or []
        build_cache = result.get("BuildCache") or []
        # ``LayersSize`` is the de-duplicated total image disk (matches
        # ``docker system df``); summing each image's ``Size`` double-counts
        # shared layers and over-reports.
        images_size = result.get("LayersSize", 0) or sum(
            img.get("Size", 0) for img in images
        )
        # ``BuilderSize`` is the legacy pre-BuildKit builder cache (0 on modern
        # Docker); the real build cache is the sum of the ``BuildCache`` records.
        build_cache_size = sum(item.get("Size", 0) for item in build_cache)
        reclaimable = sum(
            item.get("Size", 0) for item in build_cache if not item.get("InUse", True)
        )
        # Per-volume sizes from ``docker system df`` (``Volumes`` carries
        # ``UsageData.Size`` and ``RefCount``); skip the -1 "unknown" sentinel.
        volumes = [
            VolumeStat(
                name=v.get("Name", ""),
                size_bytes=(v.get("UsageData") or {}).get("Size", 0),
                in_use=(v.get("UsageData") or {}).get("RefCount", 0) > 0,
            )
            for v in (result.get("Volumes") or [])
            if (v.get("UsageData") or {}).get("Size", 0) >= 0
        ]
        return DockerDfStats(
            images_size_bytes=images_size,
            build_cache_size_bytes=build_cache_size,
            build_cache_reclaimable_bytes=reclaimable,
            volumes=volumes,
        )

    async def prune_builds(self) -> int:
        """Call Docker builder prune API and return bytes reclaimed."""
        loop = asyncio.get_running_loop()
        # all=True prunes the full build cache (not just dangling) so the button
        # frees the reclaimable space the disk panel reports, not just a few KB.
        result = await loop.run_in_executor(
            None, lambda: self._client.api.prune_builds(all=True)
        )
        return int(result.get("SpaceReclaimed", 0))

    async def prune_images(self, protected_refs: set[str]) -> int:
        """Remove dangling (untagged) images one by one, skipping protected refs.

        Docker's bulk prune API has no exclusion list, so images are removed
        individually. An image is protected when its id or any of its repo
        digests appears in *protected_refs*; images still used by a container
        fail removal with a 409, which is swallowed.
        """
        import docker  # noqa: PLC0415

        loop = asyncio.get_running_loop()

        def _prune() -> int:
            reclaimed = 0
            try:
                dangling = self._client.images.list(filters={"dangling": True})
            except docker.errors.APIError as exc:
                logger.warning("image prune: list failed: %s", exc)
                return 0
            for img in dangling:
                digests = {
                    rd.split("@")[1]
                    for rd in img.attrs.get("RepoDigests", [])
                    if "@" in rd
                }
                if img.id in protected_refs or digests & protected_refs:
                    continue
                size = int(img.attrs.get("Size", 0))
                try:
                    self._client.images.remove(img.id)
                    reclaimed += size
                except docker.errors.APIError as exc:
                    logger.debug("image prune: skipped %s: %s", img.id, exc)
            return reclaimed

        return await loop.run_in_executor(None, _prune)

    async def _find_by_config_hostname(self, hostname: str) -> Any:
        """Find the running container whose ``Config.Hostname`` is *hostname*.

        After a watchtower self-update the recreated container keeps the
        previous container's hostname (its config is copied verbatim), so the
        hostname no longer matches the container id and the direct id lookup
        misses. Returns ``None`` when no running container matches.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _scan() -> Any:
            for summary in self._client.containers.list():
                if not summary.id:
                    continue
                try:
                    full = self._client.containers.get(summary.id)
                except docker.errors.NotFound:
                    continue
                if (full.attrs.get("Config") or {}).get("Hostname") == hostname:
                    return full
            return None

        return await loop.run_in_executor(None, _scan)

    async def inspect_self(self) -> Optional[SelfInspect]:
        """Resolve the server's own container via the container-id hostname.

        Inside a container the default hostname is the short container id;
        after a watchtower self-update the hostname is the *previous*
        container's id, so a fallback scans for the container whose
        ``Config.Hostname`` matches. When both fail (custom hostname, not
        containerised, daemon unreachable) self-update is reported
        unsupported rather than raising.
        """
        import socket

        import docker

        hostname = socket.gethostname()
        try:
            container = await self._get_container(hostname)
            if container is None:
                container = await self._find_by_config_hostname(hostname)
        except docker.errors.APIError as exc:
            logger.warning("inspect_self: docker daemon unreachable: %s", exc)
            return None
        if container is None:
            return None

        attrs = container.attrs
        image_ref = (attrs.get("Config") or {}).get("Image", "")
        digest = ""
        try:
            repo_digests = (
                (container.image.attrs.get("RepoDigests") or [])
                if container.image
                else []
            )
            if repo_digests:
                digest = repo_digests[0].rsplit("@", 1)[-1]
        except docker.errors.APIError:  # Gracefully degrade; digest stays empty
            pass
        networks = list(
            ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        )
        return SelfInspect(
            container_id=attrs.get("Id", ""),
            container_name=(attrs.get("Name") or "").lstrip("/"),
            image_ref=image_ref,
            running_digest=digest,
            networks=networks,
        )

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        """Launch a one-shot watchtower container that updates *target*.

        Watchtower pulls the new image, then stops/removes the old container
        and recreates it with identical config — from outside this process,
        which is the only safe way for the server to replace itself. The
        watchtower container joins all of *target*'s networks so it reaches
        the socket proxy at *docker_host_url*, and auto-removes when done.

        ``DOCKER_API_VERSION`` must be exported: watchtower 1.7.1's client
        defaults to API 1.25, below modern daemons' minimum (1.44), and
        panics on the first API call without it. Recreating *target* also
        requires the socket proxy to allow the networks API (NETWORKS=1) —
        watchtower re-attaches the container's networks via
        ``/networks/{id}/connect``.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _run() -> str:
            api = self._client.api
            self._client.images.pull(watchtower_image)
            networking = None
            if target.networks:
                # Multi-endpoint create keeps the watchtower container itself
                # off the /networks/*/connect API path.
                networking = api.create_networking_config(
                    {net: api.create_endpoint_config() for net in target.networks}
                )
            created = api.create_container(
                image=watchtower_image,
                command=["--run-once", "--cleanup", target.container_name],
                environment={
                    "DOCKER_HOST": docker_host_url,
                    "DOCKER_API_VERSION": docker_api_version,
                },
                host_config=api.create_host_config(auto_remove=True),
                networking_config=networking,
            )
            container_id: str = created["Id"]
            api.start(container_id)
            return container_id

        try:
            return await loop.run_in_executor(None, _run)
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"failed to launch self-update container: {exc}"
            ) from exc
