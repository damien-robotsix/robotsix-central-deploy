"""RegistryChecker — polls a container registry for the latest manifest digest."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

from robotsix_http import ExternalHTTPError, RetryClient

from .._ghcr_auth import GHCR_HOST, GhcrCredentialResolver, GhcrCredentials

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    digest: str | None  # sha256:... or None on error
    fetched_at: float
    auth_error: bool = False  # True when the fetch failed due to 401/403


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
        registry.  After this call, check :meth:`was_auth_error` for whether
        the most recent fetch (or cache hit) failed due to a 401/403 auth
        rejection rather than a generic network/registry error.
        """
        entry = self._cache.get(image_ref)
        if entry and (time.monotonic() - entry.fetched_at) < self._ttl:
            return entry.digest

        digest, auth_error = await self._fetch_digest(image_ref)
        self._cache[image_ref] = _CacheEntry(
            digest=digest, fetched_at=time.monotonic(), auth_error=auth_error
        )
        return digest

    def was_auth_error(self, image_ref: str) -> bool:
        """True if the most recent fetch for *image_ref* failed due to 401/403.

        Returns ``False`` when no fetch has been attempted (or the cache has
        expired and no fresh fetch has been done).  Callers check this after
        ``get_latest_digest`` returns ``None`` to distinguish an auth
        rejection from a generic network or registry error.
        """
        entry = self._cache.get(image_ref)
        if entry and (time.monotonic() - entry.fetched_at) < self._ttl:
            return entry.auth_error
        return False

    async def _fetch_digest(self, image_ref: str) -> tuple[str | None, bool]:
        """Fetch manifest digest from registry.

        Returns ``(digest, auth_error)``.  ``auth_error`` is ``True`` when
        the fetch was rejected with 401/403, indicating that the credentials
        are missing or invalid rather than a generic network or protocol
        failure.
        """
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
                return (None, False)  # unsupported registry

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
            try:
                resp = await self._client.head(
                    url, headers=headers, follow_redirects=True
                )
            except ExternalHTTPError as exc:
                # RetryClient raises on non-2xx; without this the warning
                # below is unreachable and the failure is silent.
                if exc.status_code in (401, 403):
                    logger.warning(
                        "registry auth failed — %s returned %s for the manifest "
                        "of %s; update status stays unknown",
                        manifest_host,
                        exc.status_code,
                        image_ref,
                    )
                    return (None, True)
                return (None, False)
            if resp.status_code in (401, 403):
                # Same both-shapes guard as the token exchange above.
                logger.warning(
                    "registry auth failed — %s returned %s for the manifest "
                    "of %s; update status stays unknown",
                    manifest_host,
                    resp.status_code,
                    image_ref,
                )
                return (None, True)
            if resp.status_code not in (200, 206):
                return (None, False)
            return (resp.headers.get("Docker-Content-Digest") or None, False)
        except Exception:  # noqa: BLE001  network errors, parse errors
            return (None, False)

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

    async def _exchange_ghcr_token(
        self, repo: str, creds: GhcrCredentials | None
    ) -> str | None:
        """Run one ghcr.io token exchange for *repo* as *creds*.

        Returns the pull token, or ``None`` when the credential is rejected
        (401/403) or the exchange otherwise fails.  Rejection is logged —
        silence here is what let a revoked PAT go unnoticed for 15 days.
        """
        headers: dict[str, str] = {}
        if creds is not None:
            basic = base64.b64encode(
                f"{creds.username}:{creds.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {basic}"
        who = f"the {creds.username!r} credential" if creds else "an anonymous request"

        url = (
            f"https://{GHCR_HOST}/token"
            f"?scope=repository:{repo}:pull&service={GHCR_HOST}"
        )
        try:
            resp = await self._client.get(url, headers=headers)
        except ExternalHTTPError as exc:
            # RetryClient raises on non-2xx rather than returning the
            # response, so status checks below never see an auth failure.
            if exc.status_code in (401, 403):
                logger.warning(
                    "registry auth failed — ghcr.io rejected %s (%s) for the "
                    "token exchange on %s. %s",
                    who,
                    exc.status_code,
                    repo,
                    "Check ghcr_pull_token / the GitHub App installation."
                    if creds
                    else "The package is private; configure ghcr_pull_token "
                    "(a read:packages PAT) or the GitHub App credentials.",
                )
            return None
        except Exception:  # noqa: BLE001  network errors, parse errors
            return None

        # Belt and braces: a client configured not to raise returns the 4xx
        # instead.  Handle both shapes so the diagnostic can't go dark again.
        if resp.status_code in (401, 403):
            logger.warning(
                "registry auth failed — ghcr.io rejected %s (%s) for the "
                "token exchange on %s. %s",
                who,
                resp.status_code,
                repo,
                "Check ghcr_pull_token / the GitHub App installation."
                if creds
                else "The package is private; configure ghcr_pull_token "
                "(a read:packages PAT) or the GitHub App credentials.",
            )
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.json().get("token") or None
        except Exception:  # noqa: BLE001  malformed body
            return None

    async def _fetch_ghcr_token(self, repo: str) -> str | None:
        """Exchange a fleet GHCR credential for a pull token on *repo*.

        Tries every configured credential in preference order, then falls back
        to an anonymous exchange (which only resolves public packages).  The
        fallback matters: without it a single stale credential makes every
        private package report "unknown" even though a working one is
        configured behind it.
        """
        try:
            candidates = (
                await self._ghcr_credentials.resolve_all()
                if self._ghcr_credentials is not None
                else []
            )
        except Exception:
            logger.warning(
                "registry auth failed — could not resolve the ghcr.io credential "
                "for %s; falling back to an anonymous update check",
                repo,
                exc_info=True,
            )
            candidates = []

        for creds in candidates:
            token = await self._exchange_ghcr_token(repo, creds)
            if token:
                return token

        return await self._exchange_ghcr_token(repo, None)
