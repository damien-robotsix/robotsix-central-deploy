"""GitHub Security features endpoints for the chat agent.

Exposes:
- ``PUT /chat/github/repos/{owner}/{repo}/vulnerability-alerts`` — enable the
  Dependency graph / Dependabot alerts
- ``PUT /chat/github/repos/{owner}/{repo}/security-features`` — toggle
  repository security features (dependency graph, Dependabot alerts,
  Dependabot security updates) in one call
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_chat_agent_audit_store, _get_config
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore

from ._github_common import (
    _call_github_endpoint,
    _get_client_or_503_with_pat_fallback,
    _reraise_github_errors,
)

router = APIRouter(tags=["chat-github"])


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
    return await _call_github_endpoint(
        config,
        owner,
        repo,
        _enable_vulnerability_alerts_sync,
        audit_store=audit_store,
        audit_entry=ChatAgentAuditEntry(
            component="github",
            action="enable_vulnerability_alerts",
            key=f"{owner}/{repo}",
            new_value=True,
            detail=f"Enabled Dependency graph/vulnerability alerts on {owner}/{repo}",
        ),
    )


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
