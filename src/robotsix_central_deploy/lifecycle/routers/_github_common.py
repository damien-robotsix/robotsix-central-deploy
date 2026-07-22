"""Shared plumbing for the GitHub chat-agent routers.

Contains no route handlers, no domain-specific models or serializers.
Provides:
- All imports needed by the domain modules
- ``_get_client_or_503`` — acquire a GitHub App installation token or raise 503
- ``_get_client_or_503_with_pat_fallback`` — App token first, PAT fallback
- ``_reraise_github_errors`` — map PyGithub exceptions to HTTP status codes
- ``_call_github_endpoint`` — client acquisition + thread dispatch + error mapping
  + optional audit logging
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from fastapi import HTTPException, status

from ..config import LifecycleConfig
from ..github_app import (
    GitHubAppNotConfiguredError,
)
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

# get_github_client and get_repo_create_client are looked up lazily from
# .chat_github (inside the functions that need them) so that test-suite
# monkeypatching of chat_github.get_github_client /
# chat_github.get_repo_create_client takes effect.

_T = TypeVar("_T")


async def _get_client_or_503(config: LifecycleConfig, owner: str, repo: str) -> Any:
    from github import UnknownObjectException

    from .chat_github import get_github_client as _get_gh_client

    try:
        return await _get_gh_client(config, owner, repo)
    except UnknownObjectException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Not found: {owner}/{repo} (or the GitHub App is not installed on it)",
        )
    except GitHubAppNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


async def _get_client_or_503_with_pat_fallback(
    config: LifecycleConfig, owner: str, repo: str
) -> Any:
    """Return a GitHub client: App installation token first, PAT fallback.

    The security-features endpoint needs a credential with Administration
    scope on the target repo. The GitHub App installation token is the
    primary choice (same App that all other repo-mutation endpoints use);
    if the App is not configured we fall back to the repo-creation PAT.
    If neither credential is available the endpoint returns 503.
    """
    from github import UnknownObjectException

    from .chat_github import get_github_client as _get_gh_client
    from .chat_github import get_repo_create_client as _get_repo_create_client

    app_configured = bool(
        config.github_app_id.get_secret_value()
        and config.github_app_private_key.get_secret_value()
        and config.installation_id.get_secret_value()
    )
    pat_configured = bool(config.github_repo_create_token.get_secret_value())

    if app_configured:
        try:
            return await _get_gh_client(config, owner, repo)
        except UnknownObjectException:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Not found: {owner}/{repo} "
                "(or the GitHub App is not installed on it)",
            )

    if pat_configured:
        return _get_repo_create_client(config)

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Neither GitHub App installation token nor repo-creation PAT "
        "is configured. At least one must be set to use security-features.",
    )


def _reraise_github_errors(exc: Exception, owner: str, repo: str) -> None:
    """Map PyGithub exceptions to the matching HTTP status, else re-raise."""
    from github import GithubException, UnknownObjectException

    if isinstance(exc, UnknownObjectException):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Not found: {owner}/{repo} (or the GitHub App is not installed on it)",
        ) from exc
    if isinstance(exc, GithubException):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error: {exc}",
        ) from exc
    raise exc


async def _call_github_endpoint(
    config: LifecycleConfig,
    owner: str,
    repo: str,
    sync_fn: Any,
    *args: Any,
    audit_store: ChatAgentAuditStore | None = None,
    audit_entry: ChatAgentAuditEntry | None = None,
) -> _T:
    """Call a sync GitHub function with client acquisition and error mapping.

    Acquires the GitHub App installation client for *owner*/*repo*, runs
    *sync_fn* in a thread, maps common GitHub exceptions to HTTP status
    codes, and optionally appends an audit entry on success.
    """
    client = await _get_client_or_503(config, owner, repo)
    try:
        result = await asyncio.to_thread(sync_fn, client, owner, repo, *args)
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises
    if audit_store is not None and audit_entry is not None:
        await audit_store.append(audit_entry)
    return result  # type: ignore[no-any-return]  # asyncio.to_thread is untyped
