"""Chat-agent GitHub component: Actions status, repo read/create/update, PRs.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs`` — list recent runs
- ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}`` — a single run
- ``GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  read default workflow permissions
- ``PUT /chat/github/repos/{owner}/{repo}/actions/permissions/workflow`` —
  set default workflow permissions
- ``GET /chat/github/repos/{owner}/{repo}`` — read repo details
- ``PATCH /chat/github/repos/{owner}/{repo}`` — update repo settings
- ``PUT /chat/github/repos/{owner}/{repo}/vulnerability-alerts`` — enable the
  Dependency graph / Dependabot alerts
- ``PUT /chat/github/repos/{owner}/{repo}/security-features`` — toggle
  repository security features (dependency graph, Dependabot alerts,
  Dependabot security updates) in one call
- ``POST /chat/github/repos`` — create a new repository (Dependency graph is
  enabled automatically as part of creation)
- ``GET /chat/github/repos/{owner}/{repo}/pulls`` — list pull requests
- ``GET /chat/github/repos/{owner}/{repo}/pulls/{number}`` — a single pull request
- ``POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge`` — merge (or
  merge-queue) a pull request

This is the sole implementation of GitHub access for the chat agent — the
chat container never holds a GitHub credential of its own. Actions-status
and repo-read/update mint a GitHub App installation token server-side
(:mod:`..github_app`), the same App installation as robotsix-mill. Repo
creation alone uses a separate PAT (``github_repo_create_token``): GitHub
App installation tokens cannot create repositories under a personal
account.

Reads need no audit/confirmation gate. Repo update, security-features
toggle, and repo creation are genuine mutations, so all are audit-logged
(mirroring :mod:`.chat`'s config/restart/update endpoints) and — per the
``github`` skill's documented safety rule — expected to only be called
after the chat agent has obtained explicit user confirmation
in-conversation (a server-side confirmation gate isn't possible here;
the skill text is the enforcement point, same as the config/restart/update
endpoints in :mod:`.chat`).
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
    app_configured = bool(config.github_app_id and config.github_app_private_key)
    pat_configured = bool(config.github_repo_create_token)

    if app_configured:
        return await get_github_client(config, owner, repo)

    if pat_configured:
        return get_repo_create_client(config)

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Neither GitHub App installation token nor repo-creation PAT "
        "is configured. At least one must be set to use security-features.",
    )


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


def _pr_to_dict(pr: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``PullRequest`` to the fields the chat agent needs."""
    return {
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "draft": pr.draft,
        "user": pr.user.login if pr.user else None,
        "html_url": pr.html_url,
        "head_ref": pr.head.ref,
        "base_ref": pr.base.ref,
        "mergeable": pr.mergeable,
        "merged": pr.merged,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "created_at": pr.created_at.isoformat() if pr.created_at else None,
        "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
        "body": pr.body,
    }


def _list_pulls_sync(
    client: Any, owner: str, repo: str, state: str, per_page: int
) -> list[dict[str, Any]]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    paginated = repo_obj.get_pulls(state=state)
    return [_pr_to_dict(pr) for pr in paginated[: min(per_page, 100)]]


def _get_pull_sync(client: Any, owner: str, repo: str, number: int) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    return _pr_to_dict(repo_obj.get_pull(number))


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
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(_get_repo_sync, client, owner, repo)
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


@router.get(
    "/chat/github/repos/{owner}/{repo}/pulls",
    summary="List pull requests for a repository",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def list_pulls(
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 10,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """List *owner*/*repo*'s pull requests, most-recently-updated first.

    *state* is one of ``"open"`` (default), ``"closed"``, or ``"all"``.
    *per_page* is capped at 100 (GitHub's own page-size ceiling).
    """
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(
            _list_pulls_sync, client, owner, repo, state, per_page
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


@router.get(
    "/chat/github/repos/{owner}/{repo}/pulls/{number}",
    summary="Get a single pull request",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Pull request (or repository) not found"},
        503: {"description": "GitHub App not configured"},
    },
)
async def get_pull(
    owner: str,
    repo: str,
    number: int,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Get *owner*/*repo*'s pull request *number* (status, mergeable, URL)."""
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(_get_pull_sync, client, owner, repo, number)
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


# ---------------------------------------------------------------------------
# POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge — merge (or
# merge-queue) a pull request (write; App installation token)
# ---------------------------------------------------------------------------


class MergePullRequest(BaseModel):
    """Body for ``POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge``.

    Both fields are optional; GitHub applies the repo/PR-appropriate
    defaults when they are omitted.
    """

    merge_method: str | None = Field(
        None,
        description="Merge method: ``merge`` (default for most repos), "
        "``squash``, or ``rebase``. Omit to use the repo/PR default.",
    )
    sha: str | None = Field(
        None,
        description="Expected HEAD SHA of the pull request. When provided "
        "this is passed through to GitHub as a guard against merging a "
        "branch that has moved since the operator last reviewed it.",
    )


def _merge_pull_sync(
    client: Any, owner: str, repo: str, pull_number: int, body: MergePullRequest
) -> dict[str, Any]:
    from github import GithubObject

    repo_obj = client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)

    kwargs: dict[str, Any] = {}
    if body.merge_method:
        kwargs["merge_method"] = body.merge_method
    if body.sha:
        kwargs["sha"] = body.sha
    else:
        kwargs["sha"] = GithubObject.NotSet

    merge_status = pr.merge(**kwargs)
    return {
        "merged": merge_status.merged,
        "message": merge_status.message,
        "sha": merge_status.sha,
    }


def _merge_via_raw_requester(
    client: Any, owner: str, repo: str, pull_number: int, body: MergePullRequest
) -> dict[str, Any]:
    """Fallback merge via the raw GitHub API (used for merge-queue repos).

    When a repository requires a merge queue, PyGithub's ``pr.merge()``
    returns 405 ``Method Not Allowed``.  The raw GitHub API can still
    enqueue the PR via the same endpoint with a POST-join approach — the
    ``_requester`` bypasses PyGithub's method-guard and lets the GitHub
    API decide whether to enqueue or merge directly.
    """
    input_params: dict[str, Any] = {}
    if body.merge_method:
        input_params["merge_method"] = body.merge_method
    if body.sha:
        input_params["sha"] = body.sha

    headers, data = client._requester.requestJsonAndCheck(
        "PUT",
        f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
        input=input_params if input_params else None,
    )
    # When the merge succeeds the response data is the merge status object;
    # on failure requestJsonAndCheck raises GithubException.
    return {
        "merged": data.get("merged", False),
        "message": data.get("message", ""),
        "sha": data.get("sha", ""),
    }


def _reraise_merge_errors(
    exc: Exception, owner: str, repo: str, pull_number: int
) -> None:
    """Map PyGithub merge exceptions to the matching HTTP status, else re-raise."""
    from github import GithubException, UnknownObjectException

    if isinstance(exc, UnknownObjectException):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Not found: {owner}/{repo} pull #{pull_number} "
            "(or the GitHub App is not installed on it)",
        ) from exc
    if isinstance(exc, GithubException):
        gh_status = exc.status
        detail = str(exc)
        if gh_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Not found: {owner}/{repo} pull #{pull_number} or repo",
            ) from exc
        if gh_status == 405:
            raise HTTPException(
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                detail=f"Merge not allowed for {owner}/{repo}#{pull_number}: {detail}",
            ) from exc
        if gh_status == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Merge conflict for {owner}/{repo}#{pull_number}: {detail}",
            ) from exc
        if gh_status == 422:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"GitHub rejected merge for {owner}/{repo}#{pull_number}: {detail}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error merging {owner}/{repo}#{pull_number}: {detail}",
        ) from exc
    raise exc


@router.post(
    "/chat/github/repos/{owner}/{repo}/pulls/{pull_number}/merge",
    summary="Merge (or merge-queue) a pull request",
    responses={
        200: {"description": "Merged or enqueued successfully"},
        401: {"description": "Unauthorized"},
        404: {"description": "PR or repository not found, or App not installed on it"},
        405: {
            "description": "Merge not allowed (branch protection, merge-queue "
            "required, or the PR is not in a mergeable state)"
        },
        409: {"description": "Merge conflict"},
        422: {"description": "GitHub rejected the merge request"},
        502: {"description": "GitHub API returned an unexpected error"},
        503: {"description": "GitHub App not configured"},
    },
)
async def merge_pull(
    owner: str,
    repo: str,
    pull_number: int,
    body: MergePullRequest = MergePullRequest(merge_method=None, sha=None),
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Merge *owner*/*repo*'s pull request *pull_number*.

    The optional *merge_method* (``merge``, ``squash``, ``rebase``) and
    *sha* (expected HEAD SHA guard) are passed through to GitHub.  When
    the repository uses a merge queue the endpoint enqueues the PR
    rather than merging directly — the response ``merged`` field is
    ``true`` when the enqueue succeeded.

    Requires a write-capable credential: the GitHub App installation
    token (same as repo-update/security-features). Returns 503 if the
    App is not configured, 404 if the App is not installed on the repo,
    405 if the merge is not allowed (e.g. required status checks
    failing or merge-queue blocking), 409 on merge conflicts, and 422
    for other GitHub-side rejections.
    """
    from github import GithubException

    client = await _get_client_or_503(config, owner, repo)

    try:
        result = await asyncio.to_thread(
            _merge_pull_sync, client, owner, repo, pull_number, body
        )
    except GithubException as exc:
        if exc.status == 405:
            # 405 may mean the repo requires a merge queue — try the raw
            # requester path, which can still enqueue.
            try:
                result = await asyncio.to_thread(
                    _merge_via_raw_requester,
                    client,
                    owner,
                    repo,
                    pull_number,
                    body,
                )
            except Exception as fallback_exc:
                _reraise_merge_errors(fallback_exc, owner, repo, pull_number)
                raise  # pragma: no cover
        else:
            _reraise_merge_errors(exc, owner, repo, pull_number)
            raise  # pragma: no cover
    except Exception as exc:
        _reraise_merge_errors(exc, owner, repo, pull_number)
        raise  # pragma: no cover

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="merge_pull",
            key=f"{owner}/{repo}#{pull_number}",
            new_value={
                "merge_method": body.merge_method,
                "sha": body.sha,
                "result": result,
            },
            detail=f"Merged (or enqueued) {owner}/{repo}#{pull_number}: {result['message']}",
        )
    )
    return result


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
    client = await _get_client_or_503(config, owner, repo)
    try:
        result = await asyncio.to_thread(_update_repo_sync, client, owner, repo, body)
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="update_repo",
            key=f"{owner}/{repo}",
            new_value=body.model_dump(exclude_none=True),
            detail=f"Updated repository {result['full_name']}",
        )
    )
    return result


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
    client = await _get_client_or_503(config, owner, repo)
    try:
        return await asyncio.to_thread(
            _get_workflow_permissions_sync, client, owner, repo
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises


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
    client = await _get_client_or_503(config, owner, repo)
    try:
        result = await asyncio.to_thread(
            _set_workflow_permissions_sync, client, owner, repo, body
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises

    await audit_store.append(
        ChatAgentAuditEntry(
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
        )
    )
    return result


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/vulnerability-alerts — enable the
# Dependency graph / Dependabot alerts (write; App installation token, same
# "Administration" permission PATCH already relies on)
# ---------------------------------------------------------------------------


def _enable_vulnerability_alerts_sync(
    client: Any, owner: str, repo: str
) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    enabled = repo_obj.enable_vulnerability_alert()
    return {"full_name": repo_obj.full_name, "vulnerability_alerts_enabled": enabled}


@router.put(
    "/chat/github/repos/{owner}/{repo}/vulnerability-alerts",
    summary="Enable the Dependency graph / Dependabot vulnerability alerts",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def enable_vulnerability_alerts(
    owner: str,
    repo: str,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Enable *owner*/*repo*'s Dependency graph and Dependabot alerts.

    Without this, GitHub Actions checks that rely on the dependency graph
    (e.g. ``dependency-review-action``) fail with "Dependency review is not
    supported on this repository" until a human visits Settings > Security.
    New repos get this automatically via :func:`create_repo`; this endpoint
    covers repos that predate that, or ones created outside the chat agent.
    """
    client = await _get_client_or_503(config, owner, repo)
    try:
        result = await asyncio.to_thread(
            _enable_vulnerability_alerts_sync, client, owner, repo
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="enable_vulnerability_alerts",
            key=f"{owner}/{repo}",
            new_value=True,
            detail=f"Enabled Dependency graph/vulnerability alerts on {result['full_name']}",
        )
    )
    return result


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/security-features — toggle repository
# security features (dependency graph, Dependabot alerts, Dependabot security
# updates).  Uses the App installation token primarily; falls back to the
# repo-creation PAT when the App is not configured.
# ---------------------------------------------------------------------------


class SecurityFeaturesRequest(BaseModel):
    """Body for ``PUT /chat/github/repos/{owner}/{repo}/security-features``.

    All three fields are optional; only the ones provided are changed.
    *dependency_graph* and *dependabot_alerts* share the same underlying
    GitHub API (``vulnerability-alerts``), so setting either to ``true``
    enables vulnerability alerts and setting either to ``false`` (without
    the other being ``true``) disables them.  *dependabot_security_updates*
    controls automated security fixes independently.
    """

    dependency_graph: bool | None = Field(
        None,
        description="Enable/disable the Dependency graph "
        "(shares the vulnerability-alerts endpoint with Dependabot alerts).",
    )
    dependabot_alerts: bool | None = Field(
        None,
        description="Enable/disable Dependabot vulnerability alerts "
        "(shares the vulnerability-alerts endpoint with the Dependency graph).",
    )
    dependabot_security_updates: bool | None = Field(
        None,
        description="Enable/disable Dependabot automated security fixes.",
    )


def _set_security_features_sync(
    client: Any, owner: str, repo: str, body: SecurityFeaturesRequest
) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")

    # Vulnerability alerts: shared underlying API for both
    # dependency_graph and dependabot_alerts — true wins.
    if body.dependency_graph is True or body.dependabot_alerts is True:
        repo_obj.enable_vulnerability_alert()
    elif body.dependency_graph is False or body.dependabot_alerts is False:
        repo_obj.disable_vulnerability_alert()

    # Dependabot security updates: independent toggle.
    if body.dependabot_security_updates is True:
        repo_obj.enable_automated_security_fixes()
    elif body.dependabot_security_updates is False:
        repo_obj.disable_automated_security_fixes()

    # Re-fetch so the caller sees the resulting security_and_analysis state.
    repo_obj = client.get_repo(f"{owner}/{repo}")
    return {
        "full_name": repo_obj.full_name,
        "security_and_analysis": repo_obj.raw_data.get("security_and_analysis", {}),
    }


@router.put(
    "/chat/github/repos/{owner}/{repo}/security-features",
    summary="Toggle repository security features",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        422: {"description": "No fields provided, or GitHub rejected the request"},
        503: {
            "description": "Neither GitHub App nor PAT configured for this operation"
        },
    },
)
async def set_security_features(
    owner: str,
    repo: str,
    body: SecurityFeaturesRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Enable or disable security features on *owner*/*repo*.

    This combines what would otherwise require two separate GitHub API
    calls (``vulnerability-alerts`` and ``automated-security-fixes``)
    into one endpoint.  Only the features whose body fields are provided
    are changed; omit a field to leave it as-is.
    """
    if body.model_dump(exclude_none=True) == {}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one security feature must be provided",
        )
    client = await _get_client_or_503_with_pat_fallback(config, owner, repo)
    try:
        result = await asyncio.to_thread(
            _set_security_features_sync, client, owner, repo, body
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover — _reraise_github_errors always raises

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="set_security_features",
            key=f"{owner}/{repo}",
            new_value=body.model_dump(exclude_none=True),
            detail=f"Updated security features on {result['full_name']}",
        )
    )
    return result


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
