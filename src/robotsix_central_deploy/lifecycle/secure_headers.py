"""Gateway-aware wrapper around the security-headers middleware.

Central-deploy applies a strict Content-Security-Policy (``script-src 'self';
script-src-attr 'none'``) tuned for its own dashboard, which uses delegated
event listeners (``data-action``) and static ``.js`` files exclusively. That
CSP must **not** be injected onto gateway-proxied component responses: proxied
component UIs (mill, chat, mail, …) commonly rely on inline event-handler
attributes (``onclick`` / ``onchange``) and CDN scripts, which the strict CSP
blocks — leaving their buttons dead and side panels stuck loading. Proxied
components are responsible for their own security headers, exactly as with CSRF
(see ``GatewayAwareCSRFMiddleware``).
"""

from __future__ import annotations

from typing import Any, cast

try:
    from secure import Secure
    from secure.middleware import SecureASGIMiddleware
    from starlette.datastructures import Headers
    from starlette.types import ASGIApp, Receive, Scope, Send

    _HAS_SECURE_MW = True
except ImportError:  # pragma: no cover — optional dep
    _HAS_SECURE_MW = False


if _HAS_SECURE_MW:

    class GatewayAwareSecureMiddleware:
        """Apply security headers only to base-domain (non-proxied) responses.

        Requests whose Host is a component subdomain
        (``<name>.<gateway_base_domain>``) are gateway-proxied to a managed
        component; those responses pass through untouched so the component's
        own UI (and its own CSP, if any) is preserved. All other requests —
        central-deploy's own dashboard and API on the base domain — receive the
        full ``SecureASGIMiddleware`` header set.
        """

        def __init__(self, app: ASGIApp, *, secure: Secure) -> None:
            self._app = app
            self._secured = SecureASGIMiddleware(app, secure=secure)

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] in ("http", "websocket"):
                # Imported lazily: gateway.router pulls in lifecycle modules,
                # and this module is imported during lifecycle.app start-up.
                from ..gateway.router import _extract_subdomain_name

                headers = Headers(scope=scope)
                if _extract_subdomain_name(headers, scope.get("app")) is not None:
                    await self._app(scope, receive, send)
                    return
            await self._secured(scope, receive, cast(Any, send))
