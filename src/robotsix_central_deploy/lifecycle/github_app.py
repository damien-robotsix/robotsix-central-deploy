"""GitHub App client for the chat-agent ``github`` component.

Wraps PyGithub's ``GithubIntegration``/``Auth.AppAuth`` rather than
hand-rolling JWT signing and installation-token minting — see
robotsix-mill's ``forge/auth.py`` for the hand-rolled version this
deliberately avoids duplicating (mill ticket
``20260707T131937Z-migrate-github-app-auth-forge-auth-py-to-e1c9`` tracks
migrating mill to PyGithub too).

Shares the same GitHub App installation as robotsix-mill. The deploy server
mints the installation token server-side and never exposes the App private
key to the chat container — the chat agent only ever sees this server's own
``X-API-Key`` (same as the ``deploy`` component).

The authenticated ``Github`` client is cached per ``(app_id, owner, repo)``:
PyGithub's own ``AppInstallationAuth.token`` property lazily re-mints the
installation token only once it is close to expiry, so reusing one cached
client indefinitely avoids re-resolving the installation and re-minting a
token on every request.
"""

from __future__ import annotations

import asyncio

from .config import LifecycleConfig

_client_cache: dict[tuple[str, str, str], object] = {}


class GitHubAppNotConfiguredError(RuntimeError):
    """Raised when ``github_app_id``/``github_app_private_key`` are unset."""


def _build_client_sync(app_id: str, private_key: str, owner: str, repo: str) -> object:
    """Resolve *owner*/*repo*'s installation and return an authenticated client."""
    from github import Auth, GithubIntegration

    integration = GithubIntegration(auth=Auth.AppAuth(app_id, private_key))
    installation = integration.get_repo_installation(owner, repo)
    return integration.get_github_for_installation(installation.id)


async def get_github_client(config: LifecycleConfig, owner: str, repo: str) -> object:
    """Return a cached (or freshly-built) ``Github`` client for *owner*/*repo*.

    Raises :class:`GitHubAppNotConfiguredError` when the App id/private key
    are not configured. The returned client is typed ``object`` to keep
    ``github`` (PyGithub) import lazy — callers narrow it via ``.get_repo()``.
    """
    if not config.github_app_id or not config.github_app_private_key:
        raise GitHubAppNotConfiguredError(
            "github_app_id and github_app_private_key must both be set to "
            "use the github chat component."
        )
    key = (config.github_app_id, owner, repo)
    client = _client_cache.get(key)
    if client is None:
        client = await asyncio.to_thread(
            _build_client_sync,
            config.github_app_id,
            config.github_app_private_key,
            owner,
            repo,
        )
        _client_cache[key] = client
    return client
