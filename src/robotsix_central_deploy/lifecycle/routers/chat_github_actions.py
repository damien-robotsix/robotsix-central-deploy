"""GitHub Actions endpoints for the chat agent.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs`` — list recent runs
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}`` — a single run
- ``GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  read default workflow permissions
- ``PUT /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  set default workflow permissions
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

from ._github_common import _call_github_endpoint

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


# ---------------------------------------------------------------------------
# GET  /chat/github/repos/{owner}/{repo}/actions/permissions/workflow — read
#       workflow permissions (read; App installation token)
# PUT  /chat/github/repos/{owner}/{repo}/actions/permissions/workflow — set
#       workflow permissions (write; App installation token)
# ---------------------------------------------------------------------------


class WorkflowPermissionsRequest(BaseModel):
    """Body for ``PUT /chat/github/repos/{owner}/{repo}/actions/permissions/workflow``.

    Both fields are required by GitHub's API.
    """

    default_workflow_permissions: str = Field(
        ...,
        description="Default workflow permissions: ``read`` or ``write``.",
        pattern="^(read|write)$",
    )
    can_approve_pull_request_reviews: bool = Field(
        ...,
        description="Allow GitHub Actions to create and approve pull requests.",
    )


def _get_workflow_permissions_sync(
    client: Any, owner: str, repo: str
) -> dict[str, Any]:
    _headers, data = client._requester.requestJsonAndCheck(
        "GET", f"/repos/{owner}/{repo}/actions/permissions/workflow"
    )
    return {
        "default_workflow_permissions": data["default_workflow_permissions"],
        "can_approve_pull_request_reviews": data["can_approve_pull_request_reviews"],
    }


def _set_workflow_permissions_sync(
    client: Any, owner: str, repo: str, body: WorkflowPermissionsRequest
) -> dict[str, Any]:
    _headers, data = client._requester.requestJsonAndCheck(
        "PUT",
        f"/repos/{owner}/{repo}/actions/permissions/workflow",
        input=body.model_dump(),
    )
    return {
        "default_workflow_permissions": data["default_workflow_permissions"],
        "can_approve_pull_request_reviews": data["can_approve_pull_request_reviews"],
    }


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
    return await _call_github_endpoint(
        config, owner, repo, _list_runs_sync, branch, run_status, per_page
    )


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
    return await _call_github_endpoint(config, owner, repo, _get_run_sync, run_id)


@router.get(
    "/chat/github/repos/{owner}/{repo}/actions/permissions/workflow",
    summary="Get default workflow permissions for a repository",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def get_workflow_permissions(
    owner: str,
    repo: str,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Get *owner*/*repo*'s default workflow permissions and whether Actions
    can create and approve pull requests."""
    return await _call_github_endpoint(
        config, owner, repo, _get_workflow_permissions_sync
    )


@router.put(
    "/chat/github/repos/{owner}/{repo}/actions/permissions/workflow",
    summary="Set default workflow permissions for a repository",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        422: {"description": "GitHub rejected the request"},
        503: {"description": "GitHub App not configured"},
    },
)
async def set_workflow_permissions(
    owner: str,
    repo: str,
    body: WorkflowPermissionsRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Set *owner*/*repo*'s default workflow permissions and whether Actions
    can create and approve pull requests."""
    return await _call_github_endpoint(
        config,
        owner,
        repo,
        _set_workflow_permissions_sync,
        body,
        audit_store=audit_store,
        audit_entry=ChatAgentAuditEntry(
            component="github",
            action="set_workflow_permissions",
            key=f"{owner}/{repo}",
            new_value={
                "default_workflow_permissions": body.default_workflow_permissions,
                "can_approve_pull_request_reviews": body.can_approve_pull_request_reviews,
            },
            detail=(
                f"Updated workflow permissions on {owner}/{repo}: "
                f"default={body.default_workflow_permissions}, "
                f"can_approve_prs={body.can_approve_pull_request_reviews}"
            ),
        ),
    )
