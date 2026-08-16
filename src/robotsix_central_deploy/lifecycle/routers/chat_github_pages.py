"""GitHub Pages endpoints for the chat agent.

Exposes:
- ``PUT /chat/github/repos/{owner}/{repo}/pages`` — enable, disable, or
  reconfigure GitHub Pages on a repository
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ._github_common import _call_github_endpoint_with_pat_fallback

router = APIRouter(tags=["chat-github"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PUT /chat/github/repos/{owner}/{repo}/pages — enable, disable, or
# reconfigure GitHub Pages (write; GitHub App installation token with PAT
# fallback — the Pages API requires "Administration" / "pages: write"
# permission)
# ---------------------------------------------------------------------------


class UpdatePagesRequest(BaseModel):
    """Body for ``PUT /chat/github/repos/{owner}/{repo}/pages``.

    All fields are optional, but at least one must be provided.
    Unknown keys are rejected with 422.
    """

    model_config = {"extra": "forbid"}

    enabled: str | None = Field(
        None,
        pattern=r"^(enabled|disabled)$",
        description="``enabled`` to create (or update) the Pages site, "
        "``disabled`` to delete it.",
    )
    build_type: str | None = Field(
        None,
        pattern=r"^(workflow|legacy)$",
        description="Pages build source: ``workflow`` (GitHub Actions) or "
        "``legacy`` (branch-based).  Defaults to ``workflow`` when omitted "
        "during creation.",
    )
    source_branch: str | None = Field(
        None, description="Branch to publish from (only for ``build_type=legacy``)."
    )
    source_path: str | None = Field(
        None, description="Path within the branch (only for ``build_type=legacy``)."
    )

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> UpdatePagesRequest:
        if self.model_dump(exclude_none=True) == {}:
            raise ValueError("At least one field must be provided")
        return self


def _build_pages_input(body: UpdatePagesRequest) -> dict[str, Any]:
    """Build the ``input`` dict for the GitHub Pages API from *body*."""
    if body.build_type == "legacy":
        source: dict[str, str] = {
            "branch": body.source_branch or "main",
            "path": body.source_path or "/",
        }
        return {"source": source}
    # workflow (default) — no source dict needed
    return {"build_type": "workflow"}


def _get_pages_sync(client: Any, owner: str, repo: str) -> dict[str, Any]:
    """Fetch the current Pages site info via the raw GitHub API."""
    _headers, data = client.requester.requestJsonAndCheck(
        "GET", f"/repos/{owner}/{repo}/pages"
    )
    return data  # type: ignore[no-any-return]


def _update_pages_sync(
    client: Any, owner: str, repo: str, body: UpdatePagesRequest
) -> dict[str, Any]:
    """Enable, disable, or reconfigure GitHub Pages on *owner*/*repo*."""
    from github import GithubException

    try:
        if body.enabled == "disabled":
            # DELETE is idempotent-ish; if Pages isn't enabled, GitHub returns
            # 404 which we treat as success (already disabled).
            try:
                client.requester.requestJsonAndCheck(
                    "DELETE", f"/repos/{owner}/{repo}/pages"
                )
            except GithubException as exc:
                if exc.status != 404:
                    raise
            # After deletion, the GET returns 404 — return a synthetic
            # "disabled" response instead of calling _get_pages_sync.
            return {"full_name": f"{owner}/{repo}", "pages_enabled": False}

        # Enabled or reconfiguring: try POST (create) first; fall back to PUT
        # (update) on 409 (already exists).
        pages_input = _build_pages_input(body)
        try:
            client.requester.requestJsonAndCheck(
                "POST", f"/repos/{owner}/{repo}/pages", input=pages_input
            )
        except GithubException as exc:
            if exc.status == 409:
                client.requester.requestJsonAndCheck(
                    "PUT", f"/repos/{owner}/{repo}/pages", input=pages_input
                )
            else:
                raise

        # Return the current Pages state so the caller can verify.
        pages_data = _get_pages_sync(client, owner, repo)
        pages_data["full_name"] = f"{owner}/{repo}"
        pages_data["pages_enabled"] = True
        return pages_data
    except GithubException as exc:
        if exc.status == 403:
            data = exc.data
            if isinstance(data, dict):
                github_message = str(data.get("message") or data.get("error") or data)
            else:
                github_message = str(data) if data else str(exc)
            logger.warning(
                "GitHub Pages update for %s/%s was forbidden: %s (data=%r)",
                owner,
                repo,
                exc,
                data,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"GitHub rejected the Pages request for {owner}/{repo} "
                    f"(403): {github_message}. This may be a missing Pages "
                    "permission or a repo-level Pages restriction — check the "
                    "App installation's accepted permissions and the "
                    "repository's Pages settings."
                ),
            ) from exc
        raise


@router.put(
    "/chat/github/repos/{owner}/{repo}/pages",
    summary="Enable, disable, or reconfigure GitHub Pages",
    responses={
        401: {"description": "Unauthorized"},
        403: {"description": "GitHub App lacks required permissions"},
        404: {"description": "Repository not found or App not installed on it"},
        422: {"description": "No fields provided, or GitHub rejected the request"},
        503: {
            "description": "Neither GitHub App nor PAT configured for this operation"
        },
    },
)
async def update_pages(
    owner: str,
    repo: str,
    body: UpdatePagesRequest,
    config: LifecycleConfig = Depends(_get_config),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> dict[str, Any]:
    """Enable, disable, or reconfigure GitHub Pages on *owner*/*repo*.

    - Set ``enabled`` to ``"enabled"`` to create (or update) the Pages site.
    - Set ``enabled`` to ``"disabled"`` to delete it.
    - Omit ``enabled`` and provide ``build_type`` / ``source_branch`` /
      ``source_path`` to reconfigure an already-enabled site.

    ``build_type`` defaults to ``"workflow"`` (GitHub Actions), which is
    the expected mode for repos whose docs CI uses
    ``actions/configure-pages`` + ``actions/deploy-pages``.  Use
    ``"legacy"`` with ``source_branch`` / ``source_path`` for
    branch-based publishing.
    """
    return await _call_github_endpoint_with_pat_fallback(
        config,
        owner,
        repo,
        _update_pages_sync,
        body,
        audit_store=audit_store,
        audit_entry=ChatAgentAuditEntry(
            component="github",
            action="update_pages",
            key=f"{owner}/{repo}",
            new_value=body.model_dump(exclude_none=True),
            detail=f"Updated GitHub Pages on {owner}/{repo}",
        ),
    )
