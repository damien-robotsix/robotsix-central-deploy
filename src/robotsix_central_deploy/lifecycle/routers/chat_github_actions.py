"""GitHub Actions endpoints for the chat agent.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs`` — list recent runs
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}`` — a single run
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}/logs`` —
  workflow run logs (concatenated per-job text)
- ``GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  read default workflow permissions
- ``PUT /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  set default workflow permissions
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ..github_app import get_installation_token_sync
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

from ._github_common import _call_github_endpoint, _reraise_github_errors

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


# ---------------------------------------------------------------------------
# GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}/logs
# ---------------------------------------------------------------------------


def _tail_bytes(text: str, max_bytes: int) -> str:
    """Return the last *max_bytes* bytes of *text*, preserving valid UTF-8."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="replace")


def _fetch_and_extract_run_logs(
    token: str,
    owner: str,
    repo: str,
    run_id: int,
    *,
    job_filter: str | None = None,
    tail_kb: int = 100,
) -> str:
    """Fetch a workflow run's logs from the GitHub API and return
    concatenated per-job text.

    GitHub's run-logs endpoint returns a 302 redirect to a zip archive.
    This function follows the redirect, downloads the zip, unzips it in
    memory, and returns the per-file contents joined together.
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "robotsix-central-deploy",
    }

    from fastapi import HTTPException

    with httpx.Client(follow_redirects=False) as http_client:
        # Step 1 — request the logs endpoint (returns a 302 redirect)
        redirect_resp = http_client.get(api_url, headers=headers)
        if redirect_resp.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Run {run_id} not found in {owner}/{repo}",
            )
        if redirect_resp.status_code != 302:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"GitHub returned HTTP {redirect_resp.status_code} for run logs"
                ),
            )

        redirect_url = redirect_resp.headers.get("Location", "")
        if not redirect_url:
            raise HTTPException(
                status_code=502,
                detail="GitHub did not return a redirect URL for run logs",
            )

        # Step 2 — download the zip (pre-signed URL, no auth needed)
        zip_resp = http_client.get(redirect_url, follow_redirects=True)
        if zip_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Failed to download run logs zip: HTTP {zip_resp.status_code}"
                ),
            )

    # Step 3 — unzip and extract per-job log text
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        log_texts: list[str] = []
        for name in sorted(zf.namelist()):
            # Each entry is ``<job_name>/<step_number>_<step_name>.txt``
            if job_filter is not None:
                job_dir = name.split("/")[0] if "/" in name else name
                if job_filter not in job_dir:
                    continue

            content = zf.read(name).decode("utf-8", errors="replace")
            if tail_kb > 0:
                max_bytes = tail_kb * 1024
                if len(content.encode("utf-8")) > max_bytes:
                    content = _tail_bytes(content, max_bytes)
                    content = f"[... truncated to last {tail_kb} KB ...]\n\n{content}"

            log_texts.append(f"=== {name} ===\n{content}")

    if not log_texts:
        return "(no matching job logs found)"

    return "\n\n".join(log_texts)


@router.get(
    "/chat/github/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
    summary="Get workflow run logs (concatenated per-job text)",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run (or repository) not found"},
        503: {"description": "GitHub App not configured"},
    },
)
async def get_workflow_run_logs(
    owner: str,
    repo: str,
    run_id: int,
    job: str | None = Query(
        None,
        description="Filter logs to jobs whose name contains this string",
    ),
    tail_kb: int = Query(
        100,
        description="Only return the last N KB per job log (0 for unlimited)",
        ge=0,
    ),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> str:
    """Get *owner*/*repo*'s workflow run *run_id* logs.

    GitHub returns run logs as a zip archive.  This endpoint follows the
    redirect, unzips server-side, and returns concatenated per-job log
    text.

    *job* filters to jobs whose directory name contains the given string
    (e.g. ``"Deploy to OVH"``).
    *tail_kb* caps each job log to the last N kilobytes (default 100 KB;
    set to 0 for unlimited).
    """
    # Acquire a raw installation token (verifies App is configured and
    # the repo is covered by an installation).
    from fastapi import HTTPException

    if not config.github_app_id or not config.github_app_private_key:
        raise HTTPException(
            status_code=503,
            detail="GitHub App not configured",
        )

    try:
        token = await asyncio.to_thread(
            get_installation_token_sync,
            config.github_app_id,
            config.github_app_private_key,
            owner,
            repo,
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises

    try:
        log_text = await asyncio.to_thread(
            _fetch_and_extract_run_logs,
            token,
            owner,
            repo,
            run_id,
            job_filter=job,
            tail_kb=tail_kb,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch run logs: {exc}",
        )

    return log_text
