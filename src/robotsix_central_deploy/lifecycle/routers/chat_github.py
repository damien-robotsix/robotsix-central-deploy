"""Chat-agent GitHub Actions status (read-only) and repo creation (write).

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs`` — list recent runs
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}`` — a single run
- ``POST /chat/github/repos`` — create a new repository

The Actions-status endpoints mint a GitHub App installation token server-side
(:mod:`..github_app`) so the chat container never holds GitHub credentials —
the same GitHub App installation as robotsix-mill powers both. They are
read-only, so neither needs the chat-agent audit log or a confirmation gate
(those guard mutations elsewhere in :mod:`.chat`).

Repo creation is a genuine mutation, so it's both audit-logged (mirroring
:mod:`.chat`'s config/restart/update endpoints) and — per the ``github``
skill's documented safety rule — expected to only be called after the chat
agent has obtained explicit user confirmation in-conversation (a server-side
confirmation gate isn't possible here; the skill text is the enforcement
point, same as the config/restart/update endpoints in :mod:`.chat`). It uses
a separate PAT (``github_repo_create_token``), not the App installation
token: GitHub Apps cannot create repositories under a personal account.
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
    GitHubAppNotConfiguredError,
    GitHubRepoCreateNotConfiguredError,
    get_github_client,
    get_repo_create_client,
)
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

router = APIRouter(tags=["chat-github"])


def _run_to_dict(run: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``WorkflowRun`` to the fields the chat agent needs."""
    return {
        "id": run.id,
        "name": run.name,
        "status": run.status,
        "conclusion": run.conclusion,
        "head_branch": run.head_branch,
        "head_sha": run.head_sha,
        "run_number": run.run_number,
        "event": run.event,
        "html_url": run.html_url,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


async def _get_client_or_503(config: LifecycleConfig, owner: str, repo: str) -> Any:
    try:
        return await get_github_client(config, owner, repo)
    except GitHubAppNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _list_runs_sync(
    client: Any,
    owner: str,
    repo: str,
    branch: str | None,
    run_status: str | None,
    per_page: int,
) -> list[dict[str, Any]]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    kwargs: dict[str, str] = {}
    if branch:
        kwargs["branch"] = branch
    if run_status:
        kwargs["status"] = run_status
    paginated = repo_obj.get_workflow_runs(**kwargs)
    return [_run_to_dict(run) for run in paginated[: min(per_page, 100)]]


def _get_run_sync(client: Any, owner: str, repo: str, run_id: int) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    run = repo_obj.get_workflow_run(run_id)
    return _run_to_dict(run)


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


@router.get(
    "/chat/github/repos/{owner}/{repo}/actions/runs",
    summary="List recent GitHub Actions workflow runs for a repository",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def list_workflow_runs(
    owner: str,
    repo: str,
    branch: str | None = None,
    run_status: str | None = None,
    per_page: int = 10,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """List *owner*/*repo*'s workflow runs, most-recent-first.

    *branch* and *run_status* (GitHub's ``status`` query param — e.g.
    ``"in_progress"``, ``"completed"``, ``"queued"``) narrow the results.
    *per_page* is capped at 100 (GitHub's own page-size ceiling).
    """
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(
            _list_runs_sync, client, owner, repo, branch, run_status, per_page
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


@router.get(
    "/chat/github/repos/{owner}/{repo}/actions/runs/{run_id}",
    summary="Get a single GitHub Actions workflow run",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run (or repository) not found"},
        503: {"description": "GitHub App not configured"},
    },
)
async def get_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Get *owner*/*repo*'s workflow run *run_id* (status, conclusion, URL)."""
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(_get_run_sync, client, owner, repo, run_id)
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


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
    """
    try:
        client = get_repo_create_client(config)
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
