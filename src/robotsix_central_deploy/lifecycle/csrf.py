"""CSRF protection helpers.

Provides token generation / validation and a middleware factory that
wraps ``starlette_csrf.CSRFMiddleware``.
"""

from __future__ import annotations

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
    ``starlette_csrf.CSRFMiddleware`` so tokens are interoperable.

    When ``itsdangerous`` is not installed, operates in a no-op pass-through
    mode: ``generate`` returns a random token and ``validate`` always returns
    ``True`` (CSRF protection is effectively disabled).
    """

    def __init__(self, secret: str) -> None:
        if _HAS_ITSDANGEROUS:
            self.serializer: URLSafeSerializer | None = URLSafeSerializer(secret, "csrftoken")
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
