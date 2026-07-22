"""GitHub App client for the chat-agent ``github`` component.

Uses the shared ``robotsix-github-auth`` library for installation-token
minting rather than hand-rolling PyGithub ``GithubIntegration`` /
``Auth.AppAuth``. The deploy server mints the installation token
server-side and never exposes the App private key to the chat container —
the chat agent only ever sees this server's own ``X-API-Key`` (same as the
``deploy`` component).

The authenticated ``Github`` client is cached per ``installation_id``:
a single installation token covers all repos under that installation, so
one cached client suffices.  Token refresh is handled by minting a fresh
token on every cache miss (the library returns short-lived tokens).
"""

from __future__ import annotations

import asyncio

from .config import LifecycleConfig

_client_cache: dict[str, object] = {}


class GitHubAppNotConfiguredError(RuntimeError):
    """Raised when ``github_app_id`` / ``github_app_private_key`` /
    ``installation_id`` are unset."""


class GitHubRepoCreateNotConfiguredError(RuntimeError):
    """Raised when ``github_repo_create_token`` is unset.

    GitHub App installation tokens cannot create repositories under a
    personal account (GitHub returns 403 "Resource not accessible by
    integration"), so repo creation needs a separate PAT — see
    :data:`~robotsix_central_deploy.lifecycle.config.LifecycleConfig.github_repo_create_token`.
    """


def _bearer_client(token: str) -> object:
    """Build a PyGithub ``Github`` client authenticated with a Bearer token.

    PyGithub's ``Auth.Token`` defaults to ``Authorization: token <...>``,
    but GitHub App installation tokens require ``Authorization: Bearer
    <...>`` (the same scheme fine-grained PATs expect).
    """
    from github import Auth, Github

    class _BearerTokenAuth(Auth.Token):
        @property
        def token_type(self) -> str:
            return "Bearer"

    return Github(auth=_BearerTokenAuth(token))


def get_installation_token_sync(
    app_id: str, private_key: str, installation_id: str
) -> str:
    """Return a raw GitHub App installation access token.

    The token is suitable for use as a Bearer token or in an
    ``x-access-token`` git credential.  It is never cached — callers
    should minimise calls (the preflight handler calls this once per
    onboarding request).

    Delegates to the shared ``robotsix-github-auth`` library.
    """
    from robotsix_github_auth import mint_installation_token  # type: ignore[import-not-found]

    return mint_installation_token(app_id, private_key, installation_id)  # type: ignore[no-any-return]


async def get_github_client(config: LifecycleConfig, owner: str, repo: str) -> object:
    """Return a cached (or freshly-built) ``Github`` client.

    *owner* and *repo* are accepted for caller compatibility but are not
    used for token minting — the installation token is scoped to the
    configured ``installation_id`` and covers all repos under that
    installation.

    Raises :class:`GitHubAppNotConfiguredError` when the App id, private
    key, or installation id are not configured. The returned client is
    typed ``object`` to keep ``github`` (PyGithub) import lazy — callers
    narrow it via ``.get_repo()``.
    """
    app_id = config.github_app_id.get_secret_value()
    private_key = config.github_app_private_key.get_secret_value()
    installation_id = config.installation_id.get_secret_value()

    if not app_id or not private_key or not installation_id:
        raise GitHubAppNotConfiguredError(
            "github_app_id, github_app_private_key, and installation_id "
            "must all be set to use the github chat component."
        )

    from robotsix_github_auth import mint_installation_token  # type: ignore[import-not-found]

    client = _client_cache.get(installation_id)
    if client is None:
        token = await asyncio.to_thread(
            mint_installation_token, app_id, private_key, installation_id
        )
        client = _bearer_client(token)
        _client_cache[installation_id] = client
    return client


_repo_create_client_cache: dict[str, object] = {}


def get_repo_create_client(config: LifecycleConfig) -> object:
    """Return a cached (or freshly-built) PAT-authenticated ``Github`` client.

    Used only for repo creation: a GitHub App installation token cannot
    create repositories under a personal account, so this uses
    ``github_repo_create_token`` (a plain PAT) instead of the App-based
    client from :func:`get_github_client`. Synchronous and non-blocking —
    ``Github(auth=...)`` does no network I/O at construction time.

    Sends ``Authorization: Bearer <token>`` rather than PyGithub's
    ``Auth.Token`` default of ``Authorization: token <token>`` —
    fine-grained PATs (as opposed to classic PATs) reject the ``token``
    scheme with a 401 "Bad credentials".

    Raises :class:`GitHubRepoCreateNotConfiguredError` when the token is unset.
    """
    if not config.github_repo_create_token.get_secret_value():
        raise GitHubRepoCreateNotConfiguredError(
            "github_repo_create_token must be set to create repositories."
        )
    token = config.github_repo_create_token.get_secret_value()
    client = _repo_create_client_cache.get(token)
    if client is None:
        client = _bearer_client(token)
        _repo_create_client_cache[token] = client
    return client
