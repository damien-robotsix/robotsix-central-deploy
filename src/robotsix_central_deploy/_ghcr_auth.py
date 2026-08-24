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

Priority is a *preference*, not an exclusive choice.  :meth:`resolve_all`
returns every configured credential in that order so a caller that gets a
401/403 can fall through to the next one.  A revoked PAT used to shadow a
perfectly good App installation forever: on 2026-08-24 a ``ghp_`` token that
had been revoked 15 days earlier sat in ``system_settings.json`` (which
overlays ``config.json``) and every pull died with ``denied: denied`` while
the App credential underneath it was healthy.
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

    async def _mint_app_credentials(self) -> GhcrCredentials:
        """Mint a GitHub App installation credential.  Raises on failure."""
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

    async def resolve_all(self) -> list[GhcrCredentials]:
        """Every configured credential, most-preferred first.

        An empty list means "authenticate anonymously" — nothing is
        configured.  Callers should try each entry in turn and only give up
        on ghcr.io once all of them are rejected; that is what stops a stale
        static PAT from masking a working GitHub App installation.

        Raises ``RuntimeError`` when a GitHub App is configured, minting its
        token fails, and there is no other credential to fall back on —
        that is a misconfiguration rather than an absent credential.
        """
        candidates: list[GhcrCredentials] = []

        if self._ghcr_pull_token:
            candidates.append(
                GhcrCredentials(username="robot", password=self._ghcr_pull_token)
            )

        if self.github_app_configured:
            if not _HAS_GITHUB_AUTH:
                logger.warning(
                    "robotsix-github-auth is not installed — the GitHub App "
                    "credential for ghcr.io is unavailable"
                )
            else:
                try:
                    candidates.append(await self._mint_app_credentials())
                except RuntimeError:
                    # With a static PAT still to try, a mint failure is a
                    # degraded state, not a dead end.
                    if not candidates:
                        raise
                    logger.warning(
                        "ghcr.io: GitHub App token minting failed; falling back "
                        "to the static ghcr_pull_token only",
                        exc_info=True,
                    )

        return candidates

    async def resolve(self) -> GhcrCredentials | None:
        """Return the single most-preferred credential, or ``None``.

        Retained for callers that cannot retry.  Prefer :meth:`resolve_all`,
        which lets a rejected credential fall through to the next one.
        """
        candidates = await self.resolve_all()
        return candidates[0] if candidates else None
