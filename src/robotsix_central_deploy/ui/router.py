"""UI routes — serves the monitoring dashboard and deploy-contract help.

These routes carry no authentication dependency of their own. The dashboard is
published at the fleet's base domain by the Traefik edge, which authenticates
every request through tinyauth SSO before it arrives here — see
``registry/traefik_labels.py``. CSRF protection remains, because a valid SSO
cookie would otherwise ride along on a cross-site form post.
"""

from __future__ import annotations

import html as _html
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_CONTRACT = (Path(__file__).parent / "DEPLOY_CONTRACT.md").read_text(encoding="utf-8")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DEPLOY_CONTRACT_HTML = (_TEMPLATES_DIR / "deploy-contract.html").read_text(
    encoding="utf-8"
)


@router.get("/", include_in_schema=False)
async def root() -> Response:
    """Send the bare domain to the dashboard.

    The retired nginx vhost did this with ``location = / { return 302 /ui; }``.
    It belongs in the app rather than in the edge: the edge routes by host and
    knows nothing about which path a component serves its UI on, and putting a
    path rule there would tie the ingress to this app's layout.
    """
    return RedirectResponse(url="/ui", status_code=302)


@router.get("/ui/static/{filename:path}", include_in_schema=False)
async def ui_static(filename: str) -> FileResponse:
    """Serve a file from the static directory, with path-traversal protection.

    Resolves the real path of the requested file and verifies it starts
    with the resolved static root directory.  The os.path.realpath +
    str.startswith pattern is the canonical CodeQL-recognised sanitizer
    for py/path-injection (as used in Starlette's StaticFiles).
    """
    static_root = os.path.realpath(str(_STATIC_DIR))
    safe = os.path.realpath(os.path.join(str(_STATIC_DIR), filename))
    if not safe.startswith(static_root + os.sep):
        raise HTTPException(status_code=404)
    if not os.path.isfile(safe):
        raise HTTPException(status_code=404)
    return FileResponse(safe)


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> Response:
    """Serve the monitoring dashboard at ``GET /ui``.

    Generates a CSRF token (stored in a cookie) if one is not already
    present, injects it into the dashboard HTML, and returns an HTMLResponse.
    """
    cfg = request.app.state.config
    from ..lifecycle.csrf import get_csrf_secret

    csrf_token = request.cookies.get("csrftoken", "")
    set_cookie = False
    if not csrf_token:
        csrf_secret = get_csrf_secret(cfg.csrf_secret.get_secret_value())
        from ..lifecycle.csrf import CSRFHelper

        csrf_helper = CSRFHelper(csrf_secret)
        csrf_token = csrf_helper.generate()
        set_cookie = True
    page = _HTML.replace("{{csrf_token}}", _html.escape(csrf_token))
    page = page.replace(
        "{{gateway_base_domain}}", _html.escape(cfg.gateway_base_domain, quote=True)
    )
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
