"""UI routes — serves the monitoring dashboard and deploy-contract help."""

from __future__ import annotations

import hmac
import html as _html
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..lifecycle.auth import verify_session, _safe_next
from ..lifecycle.session import SessionStore

router = APIRouter()

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_CONTRACT = (Path(__file__).parent / "DEPLOY_CONTRACT.md").read_text(encoding="utf-8")
_LOGIN_HTML = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_auth: None = Depends(verify_session)) -> str:
    return _HTML


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/ui") -> Response:
    cfg = request.app.state.config
    if not cfg.auth_required:
        return RedirectResponse(url=_safe_next(next), status_code=303)
    token = request.cookies.get("session_token")
    store: SessionStore = request.app.state.session_store
    if token and store.validate(token):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    page = _LOGIN_HTML.replace("{{next}}", _html.escape(next)).replace("{{error}}", "")
    return HTMLResponse(content=page)


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    body = await request.body()
    params = urllib.parse.parse_qs(
        body.decode("utf-8", errors="replace"), keep_blank_values=True
    )
    username = params.get("username", [""])[0]
    password = params.get("password", [""])[0]
    next_url = _safe_next(params.get("next", ["/ui"])[0])

    cfg = request.app.state.config
    authed = False
    if not cfg.auth_required:
        authed = True
    elif cfg.auth_username and cfg.auth_password:
        authed = hmac.compare_digest(
            username, cfg.auth_username
        ) and hmac.compare_digest(password, cfg.auth_password)
    elif cfg.api_key:
        authed = hmac.compare_digest(password, cfg.api_key)

    if not authed:
        page = _LOGIN_HTML.replace("{{next}}", _html.escape(next_url)).replace(
            "{{error}}", "Invalid credentials"
        )
        return HTMLResponse(content=page, status_code=401)

    store: SessionStore = request.app.state.session_store
    token = store.create()
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=86400,
        secure=True,
    )
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    token = request.cookies.get("session_token")
    if token:
        store: SessionStore = request.app.state.session_store
        store.delete(token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="session_token", path="/")
    return response


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
