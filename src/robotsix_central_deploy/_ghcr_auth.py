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
import functools
import logging
from dataclasses import dataclass

try:
    from robotsix_github_auth import TokenMintError, mint_installation_token

    _HAS_GITHUB_AUTH = True
except ImportError:  # pragma: no cover
    mint_installation_token = None

    class TokenMintError(Exception):  # type: ignore[no-redef]
        """Fallback when ``robotsix-github-auth`` is not installed.

        Never raised by the (absent) library; defined only so the
        ``except TokenMintError`` clauses below remain valid.
        """

    _HAS_GITHUB_AUTH = False

logger = logging.getLogger(__name__)

GHCR_HOST = "ghcr.io"

# Number of consecutive GitHub App token mint failures after which
# ``GET /health`` surfaces an informational finding.
MINT_FAILURE_THRESHOLD = 3


def _parse_owner_repo_from_image_ref(image_ref: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` for a ``ghcr.io/<owner>/<name>[:tag][@digest]``
    image reference, else ``None``.

    Only the owner and the first path component (the repository/package name)
    are extracted; any tag or digest suffix is stripped.  Used to resolve the
    GitHub App installation per repository when no installation id is pinned.
    """
    ref = image_ref.strip()
    if not ref:
        return None
    # Strip any ``@sha256:...`` digest suffix, then the registry host.
    ref = ref.split("@", 1)[0]
    parts = ref.split("/")
    if parts and parts[0] == GHCR_HOST:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    owner = parts[0]
    name = parts[1].split(":", 1)[0]
    if not owner or not name:
        return None
    return owner, name


@dataclass(frozen=True)
class GhcrCredentials:
    """A registry username/password pair for ``ghcr.io``."""

    username: str
    password: str


class GhcrCredentialResolver:
    """Resolves the fleet-wide ``ghcr.io`` credentials.

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
        # Set once a pinned ``installation_id`` is rejected by GitHub so
        # subsequent mints go straight to per-repository resolution.
        self._pinned_id_rejected: bool = False
        # Consecutive failures of ``_mint_app_credentials`` (reset on success).
        self._consecutive_mint_failures: int = 0

    @property
    def github_app_configured(self) -> bool:
        """True when the GitHub App id and private key are set.

        The ``installation_id`` is optional: when empty the installation is
        resolved per repository from the image ref at mint time.
        """
        return bool(self._github_app_id and self._github_app_private_key)

    @property
    def consecutive_mint_failures(self) -> int:
        """Number of consecutive GitHub App token mint failures."""
        return self._consecutive_mint_failures

    @property
    def pull_token(self) -> str:
        """The fleet-wide ``read:packages`` PAT, or ``""`` when unset."""
        return self._ghcr_pull_token

    @pull_token.setter
    def pull_token(self, value: str) -> None:
        self._ghcr_pull_token = value.strip()

    async def _mint_app_credentials(
        self, owner: str | None = None, repo: str | None = None
    ) -> GhcrCredentials | None:
        """Mint a GitHub App installation credential.

        Returns ``None`` (and logs a WARNING) when minting cannot even be
        attempted — no installation id is pinned (or a pinned one was already
        rejected) and no ``owner``/``repo`` is available to resolve the
        installation per repository.  In that case the anonymous / static-PAT
        paths still apply.

        When a pinned ``installation_id`` is set but GitHub rejects it with a
        :class:`TokenMintError`, this retries once with per-repository
        resolution (if ``owner``/``repo`` are known), logs a single WARNING,
        and remembers the rejection so later mints skip the pinned id.

        Resets :attr:`consecutive_mint_failures` to 0 on success and
        increments it when a mint attempt ultimately fails.  Raises
        ``RuntimeError`` on an ultimate mint failure.
        """
        have_repo = bool(owner and repo)
        use_pinned = bool(self._installation_id) and not self._pinned_id_rejected

        if not use_pinned and not have_repo:
            logger.warning(
                "ghcr.io: a GitHub App is configured but no installation_id is "
                "pinned and no owner/repo could be derived from the image ref; "
                "skipping the App credential (anonymous / ghcr_pull_token paths "
                "still apply)."
            )
            return None

        loop = asyncio.get_running_loop()
        try:
            if use_pinned:
                try:
                    result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            mint_installation_token,
                            self._github_app_id,
                            self._github_app_private_key,
                            self._installation_id,
                        ),
                    )
                except TokenMintError as exc:
                    if not have_repo:
                        raise
                    result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            mint_installation_token,
                            self._github_app_id,
                            self._github_app_private_key,
                            None,
                            owner=owner,
                            repo=repo,
                        ),
                    )
                    self._pinned_id_rejected = True
                    logger.warning(
                        "ghcr.io: pinned installation_id %r was rejected by "
                        "GitHub; resolved the installation per repository "
                        "%s/%s instead. Subsequent mints skip the pinned id. "
                        "Original error: %s",
                        self._installation_id,
                        owner,
                        repo,
                        exc,
                    )
            else:
                # No usable pinned id — resolve the installation per repository.
                result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        mint_installation_token,
                        self._github_app_id,
                        self._github_app_private_key,
                        None,
                        owner=owner,
                        repo=repo,
                    ),
                )
            token: str = result.token
        except Exception as exc:
            self._consecutive_mint_failures += 1
            raise RuntimeError(
                f"Failed to mint GitHub App installation token for ghcr.io: {exc}"
            ) from exc

        self._consecutive_mint_failures = 0
        # GHCR accepts GitHub App installation tokens under the standard
        # App-token username.
        return GhcrCredentials(username="x-access-token", password=token)

    async def resolve_all(self, image_ref: str | None = None) -> list[GhcrCredentials]:
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
                owner_repo = (
                    _parse_owner_repo_from_image_ref(image_ref) if image_ref else None
                )
                owner, repo = owner_repo if owner_repo is not None else (None, None)
                try:
                    app_creds = await self._mint_app_credentials(owner, repo)
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
                else:
                    if app_creds is not None:
                        candidates.append(app_creds)

        return candidates
