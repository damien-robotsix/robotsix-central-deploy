"""RegistryChecker — polls a container registry for the latest manifest digest."""

from __future__ import annotations

import time
from dataclasses import dataclass

from robotsix_http import RetryClient


@dataclass
class _CacheEntry:
    digest: str | None  # sha256:... or None on error
    fetched_at: float


class RegistryChecker:
    """Checks whether a container image has a newer manifest in its registry."""

    def __init__(
        self,
        http_client: RetryClient,
        ttl_seconds: int = 300,
    ) -> None:
        self._client = http_client
        self._ttl = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}

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
            if first == "ghcr.io":
                repo = "/".join(segments[1:])
                manifest_host = "ghcr.io"
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
        """GET ``https://ghcr.io/token?scope=repository:<repo>:pull&service=ghcr.io``.

        Return ``.token`` on success, ``None`` on failure.
        """
        try:
            url = f"https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io"
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json().get("token") or None
        except Exception:  # noqa: BLE001
            return None
