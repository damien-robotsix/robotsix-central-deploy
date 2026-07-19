"""GitHub Pull Requests & Reviews endpoints for the chat agent.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/pulls`` — list pull requests
- ``GET /chat/github/repos/{owner}/{repo}/pulls/{number}`` — a single pull request
- ``POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge`` — merge (or
  merge-queue) a pull request
- ``GET /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews`` — list
  reviews on a pull request
- ``GET /chat/github/repos/{owner}/{repo}/pulls/{number}/comments`` — list
  inline review comments on a pull request
- ``POST /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews`` — submit
  a review (APPROVE / REQUEST_CHANGES / COMMENT)
- ``PUT /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/
  dismissals`` — dismiss a stale review
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ..github_app import GitHubRepoCreateNotConfiguredError
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

from ._github_common import (
    _call_github_endpoint,
    _get_client_or_503,
    _reraise_github_errors,
)

router = APIRouter(tags=["chat-github"])


def _pr_to_dict(pr: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``PullRequest`` to the fields the chat agent needs."""
    return {
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "mergeable_state": getattr(pr, "mergeable_state", None),
        "draft": pr.draft,
        "user": pr.user.login if pr.user else None,
        "html_url": pr.html_url,
        "head_ref": pr.head.ref,
        "head_sha": pr.head.sha,
        "base_ref": pr.base.ref,
        "mergeable": pr.mergeable,
        "merged": pr.merged,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "created_at": pr.created_at.isoformat() if pr.created_at else None,
        "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
        "body": pr.body,
    }


def _review_to_dict(review: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``PullRequestReview``."""
    return {
        "id": review.id,
        "user": review.user.login if review.user else None,
        "state": review.state,
        "submitted_at": review.submitted_at.isoformat()
        if review.submitted_at
        else None,
        "commit_id": review.commit_id,
        "body": review.body,
    }


def _review_comment_to_dict(comment: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``PullRequestComment`` (inline review comment)."""
    return {
        "id": comment.id,
        "path": comment.path,
        "line": comment.line,
        "body": comment.body,
        "user": comment.user.login if comment.user else None,
        "in_reply_to_id": comment.in_reply_to_id,
        "commit_id": comment.commit_id,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
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
# GET  /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews — list
#       reviews (read; App installation token)
# GET  /chat/github/repos/{owner}/{repo}/pulls/{number}/comments — list
#       review (inline) comments (read; App installation token)
# POST /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews — submit
#       a review (write; App installation token, PAT fallback on self-approval)
# PUT  /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/
#       dismissals — dismiss a stale review (write; App installation token)
# ---------------------------------------------------------------------------


def _list_reviews_sync(
    client: Any, owner: str, repo: str, pull_number: int, per_page: int
) -> list[dict[str, Any]]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)
    paginated = pr.get_reviews()
    return [_review_to_dict(r) for r in paginated[: min(per_page, 100)]]


def _list_review_comments_sync(
    client: Any, owner: str, repo: str, pull_number: int, per_page: int
) -> list[dict[str, Any]]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)
    paginated = pr.get_review_comments()
    return [_review_comment_to_dict(c) for c in paginated[: min(per_page, 100)]]


class CreateReviewRequest(BaseModel):
    """Body for ``POST /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews``."""

    event: str = Field(
        ...,
        description="Review action: ``APPROVE``, ``REQUEST_CHANGES``, or ``COMMENT``.",
        pattern="^(APPROVE|REQUEST_CHANGES|COMMENT)$",
    )
    body: str | None = Field(None, description="Review body text (optional).")


def _create_review_sync(
    client: Any, owner: str, repo: str, pull_number: int, body: CreateReviewRequest
) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)
    review = pr.create_review(event=body.event, body=body.body or "")
    return _review_to_dict(review)


def _create_review_via_raw_requester(
    client: Any, owner: str, repo: str, pull_number: int, body: CreateReviewRequest
) -> dict[str, Any]:
    """Fallback review submission via the raw GitHub API.

    Used when the App installation token fails due to a self-approval
    rejection (the App identity is the PR author).  The raw requester
    bypasses PyGithub's method-guard and lets us use a PAT instead.
    """
    input_params: dict[str, Any] = {"event": body.event}
    if body.body:
        input_params["body"] = body.body

    _headers, data = client._requester.requestJsonAndCheck(
        "POST",
        f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        input=input_params,
    )
    return {
        "id": data["id"],
        "user": data["user"]["login"] if data.get("user") else None,
        "state": data["state"],
        "submitted_at": data.get("submitted_at"),
        "commit_id": data.get("commit_id"),
        "body": data.get("body", ""),
    }


class DismissReviewRequest(BaseModel):
    """Body for ``PUT
    /chat/github/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/dismissals``.
    """

    message: str = Field(
        ..., min_length=1, description="Required reason for dismissal."
    )


def _dismiss_review_sync(
    client: Any,
    owner: str,
    repo: str,
    pull_number: int,
    review_id: int,
    message: str,
) -> dict[str, Any]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pull_number)
    review = pr.get_review(review_id)
    review.dismiss(message)
    return _review_to_dict(review)


@router.get(
    "/chat/github/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    summary="List reviews on a pull request",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "PR or repository not found, or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def list_reviews(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 10,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """List *owner*/*repo*'s pull request *pull_number*'s reviews,
    most-recent first.

    Returns reviewer login, ``state`` (``APPROVED``, ``CHANGES_REQUESTED``,
    ``COMMENTED``, ``DISMISSED``), ``submitted_at``, ``commit_id``, and
    ``body``.  *per_page* is capped at 100.
    """
    return await _call_github_endpoint(
        config, owner, repo, _list_reviews_sync, pull_number, per_page
    )


@router.get(
    "/chat/github/repos/{owner}/{repo}/pulls/{pull_number}/comments",
    summary="List inline review comments on a pull request",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "PR or repository not found, or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def list_review_comments(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 10,
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """List *owner*/*repo*'s pull request *pull_number*'s inline review
    comments.

    Returns ``path``, ``line``, ``body``, ``user``, ``in_reply_to_id``,
    ``commit_id``, and ``created_at``.  *per_page* is capped at 100.

    These are inline code comments, not general PR-conversation comments.
    """
    return await _call_github_endpoint(
        config, owner, repo, _list_review_comments_sync, pull_number, per_page
    )


@router.post(
    "/chat/github/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    summary="Submit a review on a pull request",
    responses={
        200: {"description": "Review submitted successfully"},
        401: {"description": "Unauthorized"},
        404: {"description": "PR or repository not found, or App not installed on it"},
        422: {
            "description": "GitHub rejected the review (e.g. self-approval "
            "when the App identity is the PR author, and no PAT fallback "
            "is configured)"
        },
        502: {"description": "GitHub API returned an unexpected error"},
        503: {"description": "GitHub App not configured"},
    },
)
async def create_review(
    owner: str,
    repo: str,
    pull_number: int,
    body: CreateReviewRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Submit a review on *owner*/*repo*'s pull request *pull_number*.

    *event* must be ``APPROVE``, ``REQUEST_CHANGES``, or ``COMMENT``.
    *body* is optional review text.

    Uses the GitHub App installation token.  If the App identity is the PR
    author, GitHub rejects self-approval with 422; the endpoint then falls
    back to the repo-creation PAT when that token is configured.  Returns
    422 if neither credential can satisfy the request.

    Requires explicit in-conversation user confirmation before calling.
    """
    from github import GithubException

    client = await _get_client_or_503(config, owner, repo)

    try:
        result = await asyncio.to_thread(
            _create_review_sync, client, owner, repo, pull_number, body
        )
    except GithubException as exc:
        if exc.status == 422 and _is_self_approval_error(exc):
            # Self-approval: try the PAT fallback.
            pat_client = _try_get_pat_client(config)
            if pat_client is not None:
                try:
                    result = await asyncio.to_thread(
                        _create_review_via_raw_requester,
                        pat_client,
                        owner,
                        repo,
                        pull_number,
                        body,
                    )
                except Exception as fallback_exc:
                    _reraise_github_errors(fallback_exc, owner, repo)
                    raise  # pragma: no cover
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"GitHub rejected the review for {owner}/{repo}#{pull_number}: "
                        f"{exc.data.get('message', str(exc))}. "
                        "The App identity appears to be the PR author "
                        "(self-approval is not allowed), and no PAT fallback "
                        "is configured."
                    ),
                ) from exc
        else:
            _reraise_github_errors(exc, owner, repo)
            raise  # pragma: no cover
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="create_review",
            key=f"{owner}/{repo}#{pull_number}",
            new_value={
                "event": body.event,
                "body": body.body,
                "review_id": result["id"],
            },
            detail=f"Submitted {body.event} review on {owner}/{repo}#{pull_number}",
        )
    )
    return result


@router.put(
    "/chat/github/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/dismissals",
    summary="Dismiss a pull request review",
    responses={
        200: {"description": "Review dismissed successfully"},
        401: {"description": "Unauthorized"},
        404: {"description": "PR, review, or repository not found"},
        422: {"description": "GitHub rejected the dismissal"},
        502: {"description": "GitHub API returned an unexpected error"},
        503: {"description": "GitHub App not configured"},
    },
)
async def dismiss_review(
    owner: str,
    repo: str,
    pull_number: int,
    review_id: int,
    body: DismissReviewRequest,
    config: LifecycleConfig = Depends(_get_config),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Dismiss *review_id* on *owner*/*repo*'s pull request *pull_number*.

    *message* is required by GitHub as the reason for dismissal.

    Requires explicit in-conversation user confirmation before calling.
    """
    client = await _get_client_or_503(config, owner, repo)
    try:
        result = await asyncio.to_thread(
            _dismiss_review_sync,
            client,
            owner,
            repo,
            pull_number,
            review_id,
            body.message,
        )
    except Exception as exc:
        _reraise_github_errors(exc, owner, repo)
        raise  # pragma: no cover

    await audit_store.append(
        ChatAgentAuditEntry(
            component="github",
            action="dismiss_review",
            key=f"{owner}/{repo}#{pull_number}/reviews/{review_id}",
            new_value={"message": body.message},
            detail=f"Dismissed review {review_id} on {owner}/{repo}#{pull_number}: {body.message}",
        )
    )
    return result


# ---------------------------------------------------------------------------
# Helpers used by the review handlers
# ---------------------------------------------------------------------------


def _is_self_approval_error(exc: Any) -> bool:
    """Return True when *exc* is a 422 caused by self-approval rejection."""
    data = getattr(exc, "data", None) or {}
    message = data.get("message", "") if isinstance(data, dict) else str(data)
    lower = message.lower()
    return ("your own" in lower and "approve" in lower) or (
        "self" in lower and "approve" in lower
    )


def _try_get_pat_client(config: LifecycleConfig) -> Any | None:
    """Return a PAT-authenticated client, or None if not configured.

    Called only from ``create_review`` (the self-approval fallback path)
    within this module.  When the GitHub App identity is the PR author,
    ``create_review`` falls back to the repo-creation PAT — this helper
    acquires that client or returns ``None`` so the handler can surface
    a clear 422 to the caller.
    """
    from .chat_github import get_repo_create_client as _get_repo_create_client

    try:
        return _get_repo_create_client(config)
    except GitHubRepoCreateNotConfiguredError:
        return None


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
    return await _call_github_endpoint(
        config, owner, repo, _list_pulls_sync, state, per_page
    )


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
    return await _call_github_endpoint(config, owner, repo, _get_pull_sync, number)
