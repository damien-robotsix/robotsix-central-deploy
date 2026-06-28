"""UI routes — serves the monitoring dashboard and deploy-contract help."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response

from ..lifecycle.auth import verify_auth

router = APIRouter()

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_CONTRACT = (Path(__file__).parent / "DEPLOY_CONTRACT.md").read_text(encoding="utf-8")


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_auth: None = Depends(verify_auth)) -> str:
    return _HTML


@router.get("/help/deploy-contract", include_in_schema=False)
def get_deploy_contract() -> Response:
    html = (
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<title>Deploy Contract</title>"
        "<style>"
        "body{background:#0f172a;color:#e2e8f0;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;"
        "margin:0;padding:20px 24px}"
        "a{color:#60a5fa}"
        "pre{white-space:pre-wrap;word-wrap:break-word}"
        ".nav{margin-bottom:16px}"
        "</style></head><body>"
        '<div class="nav"><a href="/ui">← Dashboard</a></div>'
        f"<pre>{_escape_html(_CONTRACT)}</pre>"
        "</body></html>"
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
