"""Auth guard — validates X-API-Key or HTTP Basic Auth on protected endpoints."""

from __future__ import annotations

import base64
import hmac
import urllib.parse

from fastapi import HTTPException, Request, status


async def verify_auth(request: Request) -> None:
    """FastAPI dependency that rejects requests missing valid credentials.

    Accepts either an ``X-API-Key`` header or ``Authorization: Basic``.
    The credentials are read from the app-scoped ``LifecycleConfig`` stash
    set up during server startup.
    """
    config = request.app.state.config
    if not config.auth_required:
        return  # No credentials configured — allow all (dev mode).

    # Try X-API-Key (only when api_key is configured).
    api_key = request.headers.get("X-API-Key")
    if api_key and config.api_key and _safe_compare(api_key, config.api_key):
        return

    # Try HTTP Basic Auth.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        username, password = _decode_basic_auth(auth_header)
        if config.auth_username and config.auth_password:
            # Username+password mode — both fields must match.
            if (
                username
                and password
                and _safe_compare(username, config.auth_username)
                and _safe_compare(password, config.auth_password)
            ):
                return
        elif config.api_key and password and _safe_compare(password, config.api_key):
            # Legacy api_key mode — username is ignored, password == api_key.
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Robotsix Central Deploy"'},
    )


def _decode_basic_auth(header: str) -> tuple[str, str]:
    """Decode an HTTP Basic Auth header; returns (username, password).

    Returns ("", "") on malformed input — never raises.
    """
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username, password
    except Exception:
        return "", ""


def _safe_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time (no length side-channel)."""
    return hmac.compare_digest(a, b)


async def verify_session(request: Request) -> None:
    """FastAPI dependency for browser-facing routes.

    Checks the ``session_token`` cookie. If absent or invalid, raises an
    HTTPException(303) redirecting to ``/login?next=<current-path>``.
    In dev mode (auth_required=False) it always passes.
    """
    config = request.app.state.config
    if not config.auth_required:
        return

    token = request.cookies.get("session_token")
    store = request.app.state.session_store
    if token and store.validate(token):
        return

    next_path = urllib.parse.quote(str(request.url.path), safe="")
    if request.url.query:
        next_path += "%3F" + urllib.parse.quote(request.url.query, safe="=&")
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={next_path}"},
    )


def _safe_next(next_url: str) -> str:
    """Return a safe redirect target. Rejects off-site URLs (open-redirect guard)."""
    parsed = urllib.parse.urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return "/ui"
    path = parsed.path or "/ui"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path
