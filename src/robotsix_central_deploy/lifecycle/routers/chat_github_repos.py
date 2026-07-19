"""GitHub Repository CRUD endpoints for the chat agent.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}`` — read repo details
- ``PATCH /chat/github/repos/{owner}/{repo}`` — update repo settings
- ``POST /chat/github/repos`` — create a new repository (Dependency graph is
  enabled automatically as part of creation)
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ..github_app import (
    GitHubRepoCreateNotConfiguredError,
)
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

from ._github_common import _call_github_endpoint

router = APIRouter(tags=["chat-github"])


def _repo_to_dict(repo: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``Repository`` to the fields the chat agent needs."""
    return {
        "full_name": repo.full_name,
        "html_url": repo.html_url,
        "clone_url": repo.clone_url,
        "private": repo.private,
        "description": repo.description,
        "homepage": repo.homepage,
        "has_issues": repo.has_issues,
        "has_wiki": repo.has_wiki,
        "default_branch": repo.default_branch,
        "archived": repo.archived,
    }


def _get_repo_sync(client: Any, owner: str, repo: str) -> dict[str, Any]:
    return _repo_to_dict(client.get_repo(f"{owner}/{repo}"))


@router.get(
    "/chat/github/repos/{owner}/{repo}",
    summary="Get GitHub repository details",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def get_repo(
    owner: str,
    repo: str,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Get *owner*/*repo*'s details (visibility, description, settings)."""
    return await _call_github_endpoint(config, owner, repo, _get_repo_sync)


# ---------------------------------------------------------------------------
# PATCH /chat/github/repos/{owner}/{repo} — update repo settings (write;
# App installation token — unlike creation, editing an existing repo the
# App is already installed on does not hit GitHub's personal-account
# restriction, so no separate PAT is needed here)
# ---------------------------------------------------------------------------


class UpdateRepoRequest(BaseModel):
    """Body for ``PATCH /chat/github/repos/{owner}/{repo}``.

    All fields are optional; only the ones provided are changed.
    Unknown keys are rejected with 422.
    """

    model_config = {"extra": "forbid"}

    description: str | None = Field(None, description="New description.")
    private: bool | None = Field(None, description="New visibility.")
    has_issues: bool | None = Field(None, description="Enable/disable Issues.")
    has_wiki: bool | None = Field(None, description="Enable/disable the Wiki.")
    allow_auto_merge: bool | None = Field(
        None, description="Allow auto-merge on pull requests."
    )
    delete_branch_on_merge: bool | None = Field(
        None, description="Automatically delete head branches after merge."
    )


def _update_repo_sync(
    client: Any, owner: str, repo: str, body: UpdateRepoRequest
) -> dict[str, Any]:
    from github import GithubObject

    repo_obj = client.get_repo(f"{owner}/{repo}")
    repo_obj.edit(
        description=body.description
        if body.description is not None
        else GithubObject.NotSet,
        private=body.private if body.private is not None else GithubObject.NotSet,
        has_issues=body.has_issues
        if body.has_issues is not None
        else GithubObject.NotSet,
        has_wiki=body.has_wiki if body.has_wiki is not None else GithubObject.NotSet,
        allow_auto_merge=body.allow_auto_merge
        if body.allow_auto_merge is not None
        else GithubObject.NotSet,
        delete_branch_on_merge=body.delete_branch_on_merge
        if body.delete_branch_on_merge is not None
        else GithubObject.NotSet,
    )
    repo_obj = client.get_repo(f"{owner}/{repo}")  # re-fetch to return the new state
    return _repo_to_dict(repo_obj)


@router.patch(
    "/chat/github/repos/{owner}/{repo}",
    summary="Update GitHub repository settings",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        422: {"description": "No fields provided, or GitHub rejected the request"},
        503: {"description": "GitHub App not configured"},
    },
)
async def update_repo(
    owner: str,
    repo: str,
    body: UpdateRepoRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Update *owner*/*repo*'s settings — only provided fields are changed."""
    if body.model_dump(exclude_none=True) == {}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field to update must be provided",
        )
    return await _call_github_endpoint(
        config,
        owner,
        repo,
        _update_repo_sync,
        body,
        audit_store=audit_store,
        audit_entry=ChatAgentAuditEntry(
            component="github",
            action="update_repo",
            key=f"{owner}/{repo}",
            new_value=body.model_dump(exclude_none=True),
            detail=f"Updated repository {owner}/{repo}",
        ),
    )


# ---------------------------------------------------------------------------
# POST /chat/github/repos — repo creation (write; separate PAT auth)
# ---------------------------------------------------------------------------


class CreateRepoRequest(BaseModel):
    """Body for ``POST /chat/github/repos``."""

    name: str = Field(..., description="Repository name (no owner prefix).")
    description: str = Field("", description="Repository description.")
    private: bool = Field(False, description="Create as a private repository.")
    homepage: str = Field("", description="Homepage URL.")
    topics: list[str] = Field(
        default_factory=list, description="Topics to attach after creation."
    )


def _create_repo_sync(client: Any, body: CreateRepoRequest) -> dict[str, Any]:
    user = client.get_user()
    repo = user.create_repo(
        name=body.name,
        description=body.description or "",
        homepage=body.homepage or "",
        private=body.private,
        auto_init=False,
    )
    if body.topics:
        repo.replace_topics(body.topics)
    repo.enable_vulnerability_alert()
    return {
        "full_name": repo.full_name,
        "html_url": repo.html_url,
        "clone_url": repo.clone_url,
        "private": repo.private,
        "description": repo.description,
    }


@router.post(
    "/chat/github/repos",
    summary="Create a new GitHub repository",
    responses={
        401: {"description": "Unauthorized"},
        409: {"description": "Repository already exists"},
        422: {"description": "GitHub rejected the request (e.g. invalid name)"},
        503: {"description": "Repo creation not configured"},
    },
)
async def create_repo(
    body: CreateRepoRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Create a new repository under the configured account.

    Uses ``github_repo_create_token`` (a PAT), not the GitHub App
    installation token — GitHub Apps cannot create repositories under a
    personal account. The repository is always created under that token's
    own account; there is no way to target an arbitrary owner.

    Also enables the Dependency graph / Dependabot alerts (see
    :func:`enable_vulnerability_alerts`) so CI checks that depend on it
    (e.g. ``dependency-review-action``) don't fail on day one.
    """
    try:
        from .chat_github import get_repo_create_client as _get_repo_create_client

        client = _get_repo_create_client(config)
    except GitHubRepoCreateNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    try:
        result = await asyncio.to_thread(_create_repo_sync, client, body)
    except Exception as exc:
        from github import GithubException

        if isinstance(exc, GithubException):
            detail = str(exc)
            if exc.status == 422 and "name already exists" in detail.lower():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Repository '{body.name}' already exists",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"GitHub repo create failed: {detail}",
            ) from exc
        raise

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="create_repo",
            key=body.name,
            new_value=result["html_url"],
            detail=f"Created repository {result['full_name']}",
        )
    )
    return result
