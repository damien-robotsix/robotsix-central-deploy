"""GitHub branch endpoints for the chat agent.

Exposes:
- ``GET /chat/github/repos/{owner}/{repo}/branches`` — list branches
- ``DELETE /chat/github/repos/{owner}/{repo}/branches/{branch}`` — delete a
  branch, guarded so the default and protected branches can never be removed
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ._github_common import _call_github_endpoint

router = APIRouter(tags=["chat-github"])

_PROTECTED_BRANCH_NAMES = frozenset({"main", "master"})


def _branch_to_dict(b: Any) -> dict[str, Any]:
    """Flatten a PyGithub ``Branch`` to the fields the chat agent needs."""
    return {
        "name": b.name,
        "protected": b.protected,
        "commit_sha": b.commit.sha,
    }


def _list_branches_sync(
    client: Any, owner: str, repo: str, per_page: int
) -> list[dict[str, Any]]:
    repo_obj = client.get_repo(f"{owner}/{repo}")
    paginated = repo_obj.get_branches()
    return [_branch_to_dict(b) for b in paginated[: min(per_page, 100)]]


def _delete_branch_sync(
    client: Any, owner: str, repo: str, branch: str
) -> dict[str, Any]:
    """Delete *branch* after enforcing the safety guards.

    Deletion is refused (409) when *branch* is ``main``/``master``, the
    repo's default branch, is flagged as protected, or is the head or base
    of an open pull request.  These are domain rules (not GitHub errors)
    and raise ``HTTPException`` 409 directly.  Because
    ``_reraise_github_errors`` re-raises non-``GithubException`` exceptions
    unchanged, the 409 passes through ``_call_github_endpoint`` intact.  An
    unknown branch surfaces naturally: ``get_branch`` raises
    ``UnknownObjectException`` which is mapped to 404.
    """
    repo_obj = client.get_repo(f"{owner}/{repo}")

    if branch.lower() in _PROTECTED_BRANCH_NAMES or branch == repo_obj.default_branch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Refusing to delete the default branch '{branch}' of {owner}/{repo}",
        )

    branch_obj = repo_obj.get_branch(branch)
    if branch_obj.protected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Refusing to delete protected branch '{branch}' of {owner}/{repo}",
        )

    for pull in repo_obj.get_pulls(state="open"):
        if pull.head.ref == branch or pull.base.ref == branch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Refusing to delete branch '{branch}': it belongs to "
                    f"open pull request #{pull.number}"
                ),
            )

    repo_obj.get_git_ref(f"heads/{branch}").delete()
    return {"deleted": True, "branch": branch}


@router.get(
    "/chat/github/repos/{owner}/{repo}/branches",
    summary="List branches for a repository",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found or App not installed on it"},
        503: {"description": "GitHub App not configured"},
    },
)
async def list_branches(
    owner: str,
    repo: str,
    per_page: int = 30,
    config: LifecycleConfig = Depends(_get_config),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """List *owner*/*repo*'s branches.

    Returns ``name``, ``protected``, and ``commit_sha`` per branch.
    *per_page* is capped at 100.

    Requires the GitHub App installation token.  Returns 503 if the App is
    not configured and 404 if the App is not installed on the repo.
    """
    return await _call_github_endpoint(
        config, owner, repo, _list_branches_sync, per_page
    )


@router.delete(
    "/chat/github/repos/{owner}/{repo}/branches/{branch:path}",
    summary="Delete a branch (default/protected branches refused)",
    responses={
        200: {"description": "Branch deleted successfully"},
        401: {"description": "Unauthorized"},
        404: {"description": "Branch or repository not found, or App not installed"},
        409: {
            "description": "Refused: the branch is main/master, the default "
            "branch, is protected, or belongs to an open pull request"
        },
        502: {"description": "GitHub API returned an unexpected error"},
        503: {"description": "GitHub App not configured"},
    },
)
async def delete_branch(
    owner: str,
    repo: str,
    branch: str,
    config: LifecycleConfig = Depends(_get_config),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Delete *branch* from *owner*/*repo*.

    The ``{branch:path}`` converter captures branch names containing ``/``
    (e.g. ``feature/foo``) whole.  Deletion is refused with **409** when
    *branch* is ``main``/``master``, the repository's default branch, is
    protected, or is the head or base of an open pull request — no delete
    is attempted in any of those cases.  An unknown branch returns **404**.

    Requires the GitHub App installation token.  Returns 503 if the App is
    not configured and 404 if the App is not installed on the repo.
    """
    return await _call_github_endpoint(
        config,
        owner,
        repo,
        _delete_branch_sync,
        branch,
        audit_store=audit_store,
        audit_entry=ChatAgentAuditEntry(
            component="github",
            action="delete_branch",
            key=f"{owner}/{repo}#{branch}",
            detail=f"Deleted branch '{branch}' of {owner}/{repo}",
        ),
    )
