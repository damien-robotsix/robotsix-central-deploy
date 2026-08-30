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
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LifecycleConfig

logger = logging.getLogger(__name__)

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
    parsed = parse_github_owner_repo(git_url)
    if parsed is None:
        return None
    if not (
        lifecycle_config.github_app_id.get_secret_value()
        and lifecycle_config.github_app_private_key.get_secret_value()
        and lifecycle_config.installation_id.get_secret_value()
    ):
        return None
    owner, repo = parsed
    loop = loop or asyncio.get_running_loop()
    try:
        from ..github_app import get_installation_token_sync

        return await loop.run_in_executor(
            None,
            get_installation_token_sync,
            lifecycle_config.github_app_id.get_secret_value(),
            lifecycle_config.github_app_private_key.get_secret_value(),
            lifecycle_config.installation_id.get_secret_value(),
        )
    except Exception:  # noqa: BLE001
        # owner/repo come from a regex match on a user-supplied URL;
        # sanitise to prevent log-injection (newline forgery).
        safe_owner = owner.replace("\n", "_").replace("\r", "_")
        safe_repo = repo.replace("\n", "_").replace("\r", "_")
        logger.warning(
            "Cannot get GitHub App installation token for %s/%s; "
            "cloning unauthenticated (public repos only)",
            safe_owner,
            safe_repo,
        )
        return None
