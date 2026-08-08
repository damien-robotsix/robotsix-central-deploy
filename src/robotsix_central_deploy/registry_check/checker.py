"""RegistryChecker — polls a container registry for the latest manifest digest."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

from robotsix_http import RetryClient

from .._ghcr_auth import GHCR_HOST, GhcrCredentialResolver

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    digest: str | None  # sha256:... or None on error
    fetched_at: float


class RegistryChecker:
    """Checks whether a container image has a newer manifest in its registry.

    ``ghcr_credentials`` is the fleet-wide GHCR credential resolver shared
    with the image-pull path.  Without it the checker talks to ghcr.io
    anonymously, which reports every *private* package as "unknown".
    """

    def __init__(
        self,
        http_client: RetryClient,
        ttl_seconds: int = 300,
        ghcr_credentials: GhcrCredentialResolver | None = None,
    ) -> None:
        self._client = http_client
        self._ttl = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._ghcr_credentials = ghcr_credentials

    async def get_latest_digest(self, image_ref: str) -> str | None:
        """Return cached or freshly fetched manifest digest for *image_ref*.

        Returns ``None`` on network error, non-2xx response, or unsupported
        registry.
        """
        entry = self._cache.get(image_ref)
        if entry and (time.monotonic() - entry.fetched_at) < self._ttl:
            return entry.digest

        digest = await self._fetch_digest(image_ref)
        self._cache[image_ref] = _CacheEntry(digest=digest, fetched_at=time.monotonic())
        return digest

    async def _fetch_digest(self, image_ref: str) -> str | None:
        """Fetch manifest digest from registry.  Returns ``None`` on any failure."""
        try:
            parts = image_ref.rsplit(":", 1)
            ref_no_tag = parts[0]
            tag = parts[1] if len(parts) == 2 else "latest"
            segments = ref_no_tag.split("/")
            first = segments[0]

            # --- classify registry ---
            if first == GHCR_HOST:
                repo = "/".join(segments[1:])
                manifest_host = GHCR_HOST
                token = await self._fetch_ghcr_token(repo)
            elif first == "docker.io" or ("." not in first and ":" not in first):
                if first == "docker.io":
                    repo = "/".join(segments[1:])
                elif len(segments) >= 2:
                    repo = first + "/" + "/".join(segments[1:])
                else:
                    repo = "library/" + first
                manifest_host = "registry-1.docker.io"
                token = await self._fetch_dockerhub_token(repo)
            else:
                return None  # unsupported registry

            headers = {
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                )
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            url = f"https://{manifest_host}/v2/{repo}/manifests/{tag}"
            resp = await self._client.head(url, headers=headers, follow_redirects=True)
            if resp.status_code in (401, 403):
                logger.warning(
                    "registry auth failed — %s returned %s for the manifest of %s; "
                    "update status stays unknown",
                    manifest_host,
                    resp.status_code,
                    image_ref,
                )
                return None
            if resp.status_code not in (200, 206):
                return None
            return resp.headers.get("Docker-Content-Digest") or None
        except Exception:  # noqa: BLE001  network errors, parse errors
            return None

    async def _fetch_dockerhub_token(self, repo: str) -> str | None:
        """GET anonymous pull token from Docker Hub auth service."""
        try:
            url = (
                f"https://auth.docker.io/token"
                f"?service=registry.docker.io&scope=repository:{repo}:pull"
            )
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json().get("token") or None
        except Exception:  # noqa: BLE001
            return None

    async def _fetch_ghcr_token(self, repo: str) -> str | None:
        """Exchange the fleet GHCR credential for a pull token on *repo*.

        Falls back to an anonymous exchange when no credential is configured,
        which only resolves public packages.  Returns ``None`` on failure.
        """
        headers: dict[str, str] = {}
        authenticated = False
        try:
            creds = (
                await self._ghcr_credentials.resolve()
                if self._ghcr_credentials is not None
                else None
            )
        except Exception:
            logger.warning(
                "registry auth failed — could not resolve the ghcr.io credential "
                "for %s; falling back to an anonymous update check",
                repo,
                exc_info=True,
            )
            creds = None
        if creds is not None:
            basic = base64.b64encode(
                f"{creds.username}:{creds.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {basic}"
            authenticated = True

        try:
            url = (
                f"https://{GHCR_HOST}/token"
                f"?scope=repository:{repo}:pull&service={GHCR_HOST}"
            )
            resp = await self._client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                logger.warning(
                    "registry auth failed — ghcr.io returned %s for %s token "
                    "exchange on %s. %s",
                    resp.status_code,
                    "a credentialed" if authenticated else "an anonymous",
                    repo,
                    "Check ghcr_pull_token / the GitHub App installation."
                    if authenticated
                    else "The package is private; configure ghcr_pull_token "
                    "(a read:packages PAT) or the GitHub App credentials.",
                )
                return None
            if resp.status_code != 200:
                return None
            return resp.json().get("token") or None
        except Exception:  # noqa: BLE001
            return None
