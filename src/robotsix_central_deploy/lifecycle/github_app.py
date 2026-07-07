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


class GitHubRepoCreateNotConfiguredError(RuntimeError):
    """Raised when ``github_repo_create_token`` is unset.

    GitHub App installation tokens cannot create repositories under a
    personal account (GitHub returns 403 "Resource not accessible by
    integration"), so repo creation needs a separate PAT — see
    :data:`~robotsix_central_deploy.lifecycle.config.LifecycleConfig.github_repo_create_token`.
    """


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


_repo_create_client_cache: dict[str, object] = {}


def get_repo_create_client(config: LifecycleConfig) -> object:
    """Return a cached (or freshly-built) PAT-authenticated ``Github`` client.

    Used only for repo creation: a GitHub App installation token cannot
    create repositories under a personal account, so this uses
    ``github_repo_create_token`` (a plain PAT) instead of the App-based
    client from :func:`get_github_client`. Synchronous and non-blocking —
    ``Github(auth=...)`` does no network I/O at construction time.

    Sends ``Authorization: Bearer <token>`` (matching robotsix-mill's own
    ``forge/github.py``), not PyGithub's ``Auth.Token`` default of
    ``Authorization: token <token>`` — fine-grained PATs (as opposed to
    classic PATs) reject the ``token`` scheme with a 401 "Bad credentials".

    Raises :class:`GitHubRepoCreateNotConfiguredError` when the token is unset.
    """
    if not config.github_repo_create_token:
        raise GitHubRepoCreateNotConfiguredError(
            "github_repo_create_token must be set to create repositories."
        )
    client = _repo_create_client_cache.get(config.github_repo_create_token)
    if client is None:
        from github import Auth, Github

        class _BearerTokenAuth(Auth.Token):
            @property
            def token_type(self) -> str:
                return "Bearer"

        client = Github(auth=_BearerTokenAuth(config.github_repo_create_token))
        _repo_create_client_cache[config.github_repo_create_token] = client
    return client
