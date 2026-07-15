"""CSRF protection helpers.

Provides token generation / validation and a gateway-aware wrapper around
``asgi_csrf`` (Double Submit Cookie pattern).
"""

from __future__ import annotations

import re as _re
import secrets as _secrets

try:
    from itsdangerous import BadSignature
    from itsdangerous.url_safe import URLSafeSerializer

    _HAS_ITSDANGEROUS = True
except ImportError:
    _HAS_ITSDANGEROUS = False
    import logging

    logging.getLogger(__name__).warning(
        "itsdangerous not installed; CSRF token validation disabled"
    )

try:
    from asgi_csrf import asgi_csrf as _asgi_csrf
    from starlette.datastructures import Headers
    from starlette.types import ASGIApp

    _HAS_ASGI_CSRF = True
except ImportError:
    _HAS_ASGI_CSRF = False

# Random secret if the operator doesn't supply one — regenerates on every
# restart, which invalidates any outstanding CSRF cookies.  That's an
# acceptable trade-off for a single-server deployment.
_DEFAULT_SECRET = _secrets.token_urlsafe(32)


def get_csrf_secret(explicit: str) -> str:
    """Return the CSRF secret, falling back to an auto-generated value."""
    return explicit or _DEFAULT_SECRET


class CSRFHelper:
    """Generates and validates CSRF tokens (Double Submit Cookie pattern).

    Uses the same ``itsdangerous.URLSafeSerializer`` setup as
    ``asgi_csrf`` so tokens are interoperable.

    When ``itsdangerous`` is not installed, operates in a no-op pass-through
    mode: ``generate`` returns a random token and ``validate`` always returns
    ``True`` (CSRF protection is effectively disabled).
    """

    def __init__(self, secret: str) -> None:
        if _HAS_ITSDANGEROUS:
            self.serializer: URLSafeSerializer | None = URLSafeSerializer(
                secret, "csrftoken"
            )
        else:
            self.serializer = None

    def generate(self) -> str:
        """Return a fresh signed CSRF token suitable for a cookie or form field."""
        if self.serializer is not None:
            return self.serializer.dumps(_secrets.token_urlsafe(128))
        return _secrets.token_urlsafe(128)

    def validate(self, cookie_value: str, token: str) -> bool:
        """Return ``True`` if *token* matches *cookie_value*."""
        if self.serializer is None:
            # No itsdangerous — CSRF protection unavailable; pass through.
            return True
        if not cookie_value or not token:
            return False
        try:
            decoded1: str = self.serializer.loads(cookie_value)
            decoded2: str = self.serializer.loads(token)
            return _secrets.compare_digest(decoded1, decoded2)
        except BadSignature:
            return False


if _HAS_ASGI_CSRF:

    def GatewayAwareCSRFMiddleware(
        app: ASGIApp,
        *,
        secret: str = "",
        cookie_secure: bool = False,
        cookie_samesite: str = "lax",
        exempt_urls: list[_re.Pattern[str]] | None = None,
    ) -> ASGIApp:
        """CSRF middleware that skips gateway-proxied component requests.

        ``exempt_urls`` only matches the request *path*, but the gateway
        routes components by Host subdomain (``<name>.<gateway_base_domain>``),
        so unsafe-method requests to proxied apps (e.g. a chat message POST)
        would be rejected with a CSRF token those apps never receive.
        Proxied components are responsible for their own CSRF protection.

        Returns an ASGI app wrapped with ``asgi_csrf``, configured to skip
        CSRF for both gateway-proxied subdomain requests and explicitly
        exempted URL patterns.
        """

        def _should_skip(scope: dict[str, object]) -> bool:
            # Gateway subdomain check — proxied components manage their own CSRF.
            if scope["type"] in ("http", "websocket"):
                # Imported lazily: gateway.router pulls in lifecycle modules,
                # and this module is imported during lifecycle.app start-up.
                from ..gateway.router import _extract_subdomain_name

                headers = Headers(scope=scope)
                if _extract_subdomain_name(headers, scope.get("app")) is not None:
                    return True
            # Exempt URL patterns — API routes authenticated via header-based
            # auth (X-API-Key / Basic-Auth) not vulnerable to CSRF.
            if exempt_urls and scope["type"] == "http":
                path: str = str(scope.get("path", ""))
                for pattern in exempt_urls:
                    if pattern.match(path):
                        return True
            return False

        return _asgi_csrf(  # type: ignore[no-any-return]
            app,
            signing_secret=secret,
            cookie_secure=cookie_secure,
            cookie_samesite=cookie_samesite,
            skip_if_scope=_should_skip,
        )
