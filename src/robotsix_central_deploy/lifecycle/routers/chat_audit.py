"""Chat agent audit-log endpoint.

Exposes:
- ``GET /chat/audit-log`` — read recent audit entries
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import verify_auth
from ..deps import _get_chat_agent_audit_store
from ..schemas import ChatAgentAuditEntryResponse, ChatAgentAuditLogResponse
from ...registry.chat_agent_audit_store import ChatAgentAuditStore

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# GET /chat/audit-log
# ---------------------------------------------------------------------------


@router.get(
    "/chat/audit-log",
    response_model=ChatAgentAuditLogResponse,
    summary="Read recent chat-agent mutation audit entries",
    responses={401: {"description": "Unauthorized"}},
)
async def chat_audit_log(
    request: Request,
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    limit: int = 50,
    component: str | None = None,
    _auth: None = Depends(verify_auth),
) -> ChatAgentAuditLogResponse:
    """Return recent chat-agent audit entries, most-recent-first.

    Optionally filter by *component* name.
    """
    entries = await audit_store.list(limit=limit, component=component)
    return ChatAgentAuditLogResponse(
        entries=[
            ChatAgentAuditEntryResponse(
                timestamp=e.timestamp,
                agent_id=e.agent_id,
                component=e.component,
                action=e.action,
                key=e.key,
                old_value=e.old_value,
                new_value=e.new_value,
                detail=e.detail,
            )
            for e in entries
        ]
    )
