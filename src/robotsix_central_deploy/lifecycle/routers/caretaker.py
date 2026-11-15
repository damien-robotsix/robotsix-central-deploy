"""Caretaker status endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...caretaker.scheduler import CaretakerScheduler
from ..auth import verify_auth

router = APIRouter(tags=["caretaker"])


async def _get_caretaker_scheduler(request: Request) -> CaretakerScheduler:
    """Dependency: return the CaretakerScheduler from app state."""
    scheduler = request.app.state.caretaker_scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=500, detail="caretaker_scheduler not initialised"
        )
    return scheduler  # type: ignore[no-any-return]


@router.get("/caretaker/status")
async def get_caretaker_status(
    _auth: None = Depends(verify_auth),
    scheduler: CaretakerScheduler = Depends(_get_caretaker_scheduler),
) -> dict[str, Any]:
    """Return the current caretaker status."""
    return await scheduler.get_status()
