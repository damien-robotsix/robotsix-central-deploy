"""UI routes — serves the monitoring dashboard and deploy-contract help."""

from __future__ import annotations

import hmac
import html as _html
import os
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..lifecycle.auth import verify_session, _safe_next
from ..lifecycle.session import SessionStore

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_CONTRACT = (Path(__file__).parent / "DEPLOY_CONTRACT.md").read_text(encoding="utf-8")
_LOGIN_HTML = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DEPLOY_CONTRACT_HTML = (_TEMPLATES_DIR / "deploy-contract.html").read_text(
    encoding="utf-8"
)


@router.get("/ui/static/{filename:path}", include_in_schema=False)
async def ui_static(filename: str) -> FileResponse:
    """Serve a file from the static directory, with path-traversal protection.

    Resolves the real path of the requested file and verifies it starts
    with the resolved static root directory.  The os.path.realpath +
    str.startswith pattern is the canonical CodeQL-recognised sanitizer
    for py/path-injection (as used in Starlette's StaticFiles).
    """
    static_root = os.path.realpath(str(_STATIC_DIR))
    safe = os.path.realpath(
        os.path.join(str(_STATIC_DIR), filename)
    )  # codeql[py/path-injection]: path-traversal guarded by realpath + startswith above
    if not safe.startswith(static_root + os.sep):
        raise HTTPException(status_code=404)
    if not os.path.isfile(safe):
        raise HTTPException(status_code=404)
    return FileResponse(safe)


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request, _auth: None = Depends(verify_session)
) -> Response:
    """Serve the monitoring dashboard at ``GET /ui``.

    Generates a CSRF token (stored in a cookie) if one is not already
    present, injects it into the dashboard HTML, and returns an HTMLResponse.
    """
    cfg = request.app.state.config
    from ..lifecycle.csrf import get_csrf_secret

    csrf_token = request.cookies.get("csrftoken", "")
    set_cookie = False
    if not csrf_token:
        csrf_secret = get_csrf_secret(cfg.csrf_secret)
        from ..lifecycle.csrf import CSRFHelper

        csrf_helper = CSRFHelper(csrf_secret)
        csrf_token = csrf_helper.generate()
        set_cookie = True
    page = _HTML.replace("{{csrf_token}}", _html.escape(csrf_token))
    response: Response = HTMLResponse(content=page)
    if set_cookie:
        response.set_cookie(
            key="csrftoken",
            value=csrf_token,
            httponly=True,
            samesite="lax",
            path="/",
            secure=True,
        )
    return response


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/ui") -> Response:
    """Serve the login form at ``GET /login``.

    If authentication is not required or a valid session token is present,
    redirects to *next*.  Otherwise renders the login page with a fresh
    CSRF token set as a cookie.  Returns an HTMLResponse or RedirectResponse.
    """
    cfg = request.app.state.config
    if not cfg.auth_required:
        # codeql[py/url-redirection]: target sanitized by _safe_next (rejects scheme/netloc)
        return RedirectResponse(
            url=_safe_next(next),
            status_code=303,
        )
    token = request.cookies.get("session_token")
    store: SessionStore = request.app.state.session_store
    if token and store.validate(token):
        # codeql[py/url-redirection]: target sanitized by _safe_next (rejects scheme/netloc)
        return RedirectResponse(
            url=_safe_next(next),
            status_code=303,
        )

    # --- CSRF token -------------------------------------------------------
    from ..lifecycle.csrf import CSRFHelper, get_csrf_secret

    csrf_secret = get_csrf_secret(cfg.csrf_secret)
    csrf_helper = CSRFHelper(csrf_secret)
    csrf_token = csrf_helper.generate()

    page = (
        _LOGIN_HTML.replace("{{next}}", _html.escape(next))
        .replace("{{error}}", "")
        .replace("{{csrf_token}}", _html.escape(csrf_token))
    )
    response: Response = HTMLResponse(content=page)
    response.set_cookie(
        key="csrftoken",
        value=csrf_token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=True,
    )
    return response


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    """Handle login form submission at ``POST /login``.

    Validates the CSRF token and credentials (username/password or API key).
    On success sets a session cookie and redirects to *next*; on failure
    re-renders the login form with an error message and a fresh CSRF cookie.
    Returns an HTMLResponse or RedirectResponse.
    """
    body = await request.body()
    params = urllib.parse.parse_qs(
        body.decode("utf-8", errors="replace"), keep_blank_values=True
    )
    username = params.get("username", [""])[0]
    password = params.get("password", [""])[0]
    next_url = _safe_next(params.get("next", ["/ui"])[0])

    cfg = request.app.state.config

    # --- CSRF validation --------------------------------------------------
    from ..lifecycle.csrf import CSRFHelper, get_csrf_secret

    csrf_secret = get_csrf_secret(cfg.csrf_secret)
    csrf_helper = CSRFHelper(csrf_secret)
    cookie_token = request.cookies.get("csrftoken", "")
    form_token = params.get("csrftoken", [""])[0]
    if not csrf_helper.validate(cookie_token, form_token):
        page = (
            _LOGIN_HTML.replace("{{next}}", _html.escape(next_url))
            .replace(
                "{{error}}",
                "CSRF token validation failed — please reload the page and try again.",
            )
            .replace(
                "{{csrf_token}}", _html.escape(cookie_token or csrf_helper.generate())
            )
        )
        return HTMLResponse(content=page, status_code=403)

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
        # Generate a fresh CSRF token for the re-displayed login form
        fresh_csrf = csrf_helper.generate()
        page = (
            _LOGIN_HTML.replace("{{next}}", _html.escape(next_url))
            .replace("{{error}}", "Invalid credentials")
            .replace("{{csrf_token}}", _html.escape(fresh_csrf))
        )
        response: Response = HTMLResponse(content=page, status_code=401)
        response.set_cookie(
            key="csrftoken",
            value=fresh_csrf,
            httponly=True,
            samesite="lax",
            path="/",
            secure=True,
        )
        return response

    store: SessionStore = request.app.state.session_store
    token = store.create()
    # codeql[py/url-redirection]: target sanitized by _safe_next (rejects scheme/netloc)
    response = RedirectResponse(
        url=next_url,
        status_code=303,
    )
    # Share the session cookie across component subdomains so a login on the
    # base domain also authorizes subdomain-routed component UIs
    # (e.g. mail.<gateway_base_domain>/...). Host-only when no base domain set.
    cookie_domain = f".{cfg.gateway_base_domain}" if cfg.gateway_base_domain else None
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=86400,
        secure=True,
        domain=cookie_domain,
    )
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    """Handle logout at ``POST /logout``.

    Deletes the session token from the store and clears the session cookie,
    then redirects to ``/login``.  Returns a RedirectResponse.
    """
    token = request.cookies.get("session_token")
    if token:
        store: SessionStore = request.app.state.session_store
        store.delete(token)
    cfg = request.app.state.config
    cookie_domain = f".{cfg.gateway_base_domain}" if cfg.gateway_base_domain else None
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="session_token", path="/", domain=cookie_domain)
    return response


@router.get("/help/deploy-contract", include_in_schema=False)
def get_deploy_contract() -> Response:
    """Serve the deploy-contract help page at ``GET /help/deploy-contract``.

    Renders the pre-loaded ``DEPLOY_CONTRACT.md`` into an HTML page with
    escaped content.  Returns a ``Response`` with ``text/html`` media type.
    """
    html = _DEPLOY_CONTRACT_HTML.replace("{{ contract }}", _escape_html(_CONTRACT))
    return Response(content=html, media_type="text/html; charset=utf-8")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
