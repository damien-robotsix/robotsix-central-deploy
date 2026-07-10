"""Claude-auth credential management for the Docker SDK backend."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

CLAUDE_AUTH_VOLUME = "claude-auth"


class AuthOps:
    """Stateful helper for Claude auth credential CRUD operations.

    Shares the Docker client with the owning ``DockerSdkBackend``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def check_claude_credentials(self) -> list[str]:
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

            # Check for expiry information. Claude Code stores OAuth tokens
            # under "claudeAiOauth" with "expiresAt" as a ms epoch; older
            # formats used a top-level ISO "expires_at".
            oauth = creds.get("claudeAiOauth") or {}
            has_refresh = bool(oauth.get("refreshToken"))
            expires_at = (
                oauth.get("expiresAt")
                or creds.get("expires_at")
                or creds.get("expiresAt")
            )
            if expires_at:
                try:
                    from datetime import datetime, timezone

                    if isinstance(expires_at, (int, float)):
                        expire_dt = datetime.fromtimestamp(
                            expires_at / 1000.0, tz=timezone.utc
                        )
                    else:
                        expire_dt = datetime.fromisoformat(
                            str(expires_at).replace("Z", "+00:00")
                        )
                    now = datetime.now(timezone.utc)
                    if expire_dt < now:
                        if has_refresh:
                            return {
                                "status": "authenticated",
                                "detail": "Access token expired; refreshes on next use.",
                            }
                        return {
                            "status": "not-authenticated",
                            "detail": "Credentials have expired.",
                        }
                    remaining = (expire_dt - now).total_seconds()
                    if remaining < 86400 and not has_refresh:  # less than 1 day
                        return {
                            "status": "expiring",
                            "detail": f"Credentials expire in {remaining / 3600:.1f} hours.",
                        }
                except ValueError, TypeError, OSError, OverflowError:
                    pass  # unparseable expiry → treat as valid

            return {"status": "authenticated"}

        return await loop.run_in_executor(None, _check)

    async def write_claude_credentials(
        self, volume_name: str, credentials_json: str
    ) -> dict[str, Any]:
        """Write *credentials_json* into *volume_name* as ``.credentials.json``."""
        import docker
        import json as _json
        import base64

        # Validate that it's at least parseable JSON.
        try:
            _json.loads(credentials_json)
        except _json.JSONDecodeError as exc:
            return {"status": "error", "error": f"Invalid JSON: {exc}"}

        loop = asyncio.get_running_loop()

        def _write() -> dict[str, Any]:
            # Ensure the volume exists (create + chown on first use).
            try:
                self._client.volumes.get(volume_name)
            except docker.errors.NotFound:
                self._client.volumes.create(volume_name)
                self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "chown 1000:1000 /mnt && chmod 700 /mnt",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
                    remove=True,
                )

            encoded = credentials_json.encode("utf-8")
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

    async def read_claude_credentials(self, volume_name: str) -> dict[str, Any]:
        """Read and return the parsed ``.credentials.json`` from *volume_name*."""
        import docker
        import json as _json

        loop = asyncio.get_running_loop()

        def _read() -> dict[str, Any]:
            try:
                self._client.volumes.get(volume_name)
            except docker.errors.NotFound:
                raise ValueError(f"Volume '{volume_name}' does not exist.")

            try:
                raw = self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "cat /mnt/.credentials.json 2>/dev/null || echo 'MISSING'",
                    ],
                    volumes={volume_name: {"bind": "/mnt", "mode": "ro"}},
                    remove=True,
                )
                content = raw.decode("utf-8", errors="replace").strip()
            except docker.errors.ContainerError:
                raise ValueError("Failed to read credentials from volume.")

            if content == "MISSING" or not content:
                raise ValueError("No credentials file found.")

            try:
                result: Any = _json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("Credentials file is not a JSON object.")
                return result
            except _json.JSONDecodeError as exc:
                raise ValueError(f"Credentials file is not valid JSON: {exc}")

        return await loop.run_in_executor(None, _read)
