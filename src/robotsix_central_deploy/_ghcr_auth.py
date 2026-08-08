"""Fleet-wide GHCR credential resolution.

Two paths talk to ``ghcr.io``: the image pull (Docker SDK auth config) and
the update check (Registry v2 token exchange).  They must present the same
identity — when only the pull authenticated, every private package reported
its update status as "unknown" while pulling fine.  Both now resolve their
credential here.

Priority, unchanged from the pull path:

1. Static ``ghcr_pull_token`` (a ``read:packages`` PAT) — set via the config UI.
2. A GitHub App installation token, minted via ``robotsix-github-auth``.
3. Nothing — anonymous, which works for public packages only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

try:
    from robotsix_github_auth import mint_installation_token

    _HAS_GITHUB_AUTH = True
except ImportError:  # pragma: no cover
    mint_installation_token = None
    _HAS_GITHUB_AUTH = False

logger = logging.getLogger(__name__)

GHCR_HOST = "ghcr.io"


@dataclass(frozen=True)
class GhcrCredentials:
    """A registry username/password pair for ``ghcr.io``."""

    username: str
    password: str


class GhcrCredentialResolver:
    """Resolves the single fleet-wide ``ghcr.io`` credential.

    One instance is shared by the pull path and the update-check path, so a
    token updated at runtime (``PUT /services/central-deploy/env``) takes
    effect on both at once.
    """

    def __init__(
        self,
        github_app_id: str = "",
        github_app_private_key: str = "",
        installation_id: str = "",
        ghcr_pull_token: str = "",
    ) -> None:
        self._github_app_id = github_app_id.strip()
        self._github_app_private_key = github_app_private_key.strip()
        self._installation_id = installation_id.strip()
        self._ghcr_pull_token = ghcr_pull_token.strip()

    @property
    def github_app_configured(self) -> bool:
        """True when all three GitHub App fields are set."""
        return bool(
            self._github_app_id
            and self._github_app_private_key
            and self._installation_id
        )

    @property
    def pull_token(self) -> str:
        """The fleet-wide ``read:packages`` PAT, or ``""`` when unset."""
        return self._ghcr_pull_token

    @pull_token.setter
    def pull_token(self, value: str) -> None:
        self._ghcr_pull_token = value.strip()

    async def resolve(self) -> GhcrCredentials | None:
        """Return the credential to present to ``ghcr.io``, or ``None``.

        ``None`` means "authenticate anonymously" — no credential is
        configured.  Raises ``RuntimeError`` when a GitHub App *is*
        configured but minting its installation token fails, since that is
        a misconfiguration rather than an absent credential.
        """
        if self._ghcr_pull_token:
            return GhcrCredentials(username="robot", password=self._ghcr_pull_token)

        if not self.github_app_configured:
            return None
        if not _HAS_GITHUB_AUTH:
            logger.warning(
                "robotsix-github-auth is not installed — ghcr.io access will be anonymous"
            )
            return None

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                mint_installation_token,
                self._github_app_id,
                self._github_app_private_key,
                self._installation_id,
            )
            token: str = result.token
        except Exception as exc:
            raise RuntimeError(
                f"Failed to mint GitHub App installation token for ghcr.io: {exc}"
            ) from exc

        # GHCR accepts GitHub App installation tokens under the standard
        # App-token username.
        return GhcrCredentials(username="x-access-token", password=token)
