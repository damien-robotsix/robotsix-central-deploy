"""GitHub App installation token for cloning a component's repo.

Shared by onboarding (``_compose_resolver``) and contract refresh
(``seed._fetch_component_repo_files``): both clone ``deploy/docker-compose.yml``
from the component's repo, and a private repo needs the App token as an
``x-access-token`` credential.  Until the refresh path used this helper it
cloned anonymously, so every private component (observed 2026-08-30 on
``hexarchy``) kept its stale onboard-time contract — ``ports: []`` → no
Traefik labels → the proxy answered ``404 page not found`` while the container
was healthy — and ``refresh-contract`` / deploy-time refresh failed with
``could not read Username for 'https://github.com'``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LifecycleConfig

try:
    from robotsix_github_auth import TokenMintError

    _HAS_GITHUB_AUTH = True
except ImportError:  # pragma: no cover

    class TokenMintError(Exception):  # type: ignore[no-redef]
        """Fallback when ``robotsix-github-auth`` is not installed."""

    _HAS_GITHUB_AUTH = False

logger = logging.getLogger(__name__)

# Set once a pinned ``installation_id`` is rejected by GitHub so subsequent
# clones go straight to per-repository resolution (per-process memory only).
_pinned_id_rejected = False

_GITHUB_URL_RE = re.compile(
    r"^https://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def parse_github_owner_repo(git_url: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` for a GitHub https URL, else ``None``."""
    m = _GITHUB_URL_RE.match(git_url.strip())
    return (m.group(1), m.group(2)) if m else None


async def github_token_for_repo(
    git_url: str,
    lifecycle_config: LifecycleConfig,
    loop: asyncio.AbstractEventLoop | None = None,
) -> str | None:
    """Mint an installation token for cloning *git_url*, or ``None``.

    ``None`` when the URL is not GitHub, the App is not configured, or
    minting fails (logged; the caller then clones unauthenticated, which
    still works for public repos).
    """
    global _pinned_id_rejected
    parsed = parse_github_owner_repo(git_url)
    if parsed is None:
        return None
    app_id = lifecycle_config.github_app_id.get_secret_value()
    private_key = lifecycle_config.github_app_private_key.get_secret_value()
    if not (app_id and private_key):
        return None
    installation_id = lifecycle_config.installation_id.get_secret_value()
    owner, repo = parsed
    # owner/repo come from a regex match on a user-supplied URL; sanitise
    # to prevent log-injection (newline forgery) before any logging.
    safe_owner = owner.replace("\n", "_").replace("\r", "_")
    safe_repo = repo.replace("\n", "_").replace("\r", "_")
    loop = loop or asyncio.get_running_loop()
    try:
        from ..github_app import get_installation_token_sync

        use_pinned = bool(installation_id) and not _pinned_id_rejected
        if use_pinned:
            try:
                return await loop.run_in_executor(
                    None,
                    functools.partial(
                        get_installation_token_sync,
                        app_id,
                        private_key,
                        installation_id,
                    ),
                )
            except TokenMintError as exc:
                # Pinned id rejected — resolve the installation per repository
                # once and remember it for subsequent clones.
                token = await loop.run_in_executor(
                    None,
                    functools.partial(
                        get_installation_token_sync,
                        app_id,
                        private_key,
                        "",
                        owner=owner,
                        repo=repo,
                    ),
                )
                _pinned_id_rejected = True
                logger.warning(
                    "GitHub App pinned installation_id was rejected by GitHub; "
                    "resolved the installation per repository %s/%s instead "
                    "(subsequent clones skip the pinned id). Original error: %s",
                    safe_owner,
                    safe_repo,
                    exc,
                )
                return token
        # No usable pinned id (empty, or one GitHub already rejected) —
        # resolve the installation per repository.
        return await loop.run_in_executor(
            None,
            functools.partial(
                get_installation_token_sync,
                app_id,
                private_key,
                "",
                owner=owner,
                repo=repo,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Cannot get GitHub App installation token for %s/%s; "
            "cloning unauthenticated (public repos only)",
            safe_owner,
            safe_repo,
        )
        return None
