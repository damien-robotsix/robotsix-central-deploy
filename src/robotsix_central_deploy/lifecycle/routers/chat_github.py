"""Chat-agent GitHub Actions workflow-run status (read-only).

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs`` — list recent runs
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}`` — a single run

Both endpoints mint a GitHub App installation token server-side
(:mod:`..github_app`) so the chat container never holds GitHub credentials —
the same GitHub App installation as robotsix-mill powers both. Read-only, so
neither endpoint needs the chat-agent audit log or a confirmation gate (those
guard mutations elsewhere in :mod:`.chat`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_config
from ..github_app import GitHubAppNotConfiguredError, get_github_client

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
