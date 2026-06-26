"""UI routes — serves the monitoring dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..lifecycle.auth import verify_auth

router = APIRouter()

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_auth: None = Depends(verify_auth)) -> str:
    return _HTML
