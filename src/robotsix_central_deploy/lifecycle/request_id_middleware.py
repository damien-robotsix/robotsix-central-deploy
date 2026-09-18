"""Request-ID / correlation-ID tracking middleware.

Reads an inbound ``X-Request-ID`` header (or generates a UUID4 when the
client did not supply one), binds it onto :mod:`structlog.contextvars` under
the ``request_id`` key so that any code running within the request —
including structlog/stdlib log records — can correlate its output, and
echoes the value back to the client in the ``X-Request-ID`` response header.

The bound value is exposed via :func:`get_request_id` and merged onto every
JSON log record by ``structlog.contextvars.merge_contextvars`` (enabled by
``robotsix_llmio.logging.setup_structlog(correlation_id=True)`` in
:mod:`._logging`), which stamps every record emitted during a request with
its ``request_id``.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import (
    bind_contextvars,
    get_contextvars,
    unbind_contextvars,
)

# Canonical header name for the correlation id (both directions).
REQUEST_ID_HEADER = "X-Request-ID"

# Key under which the correlation id is bound in the structlog context and
# rendered onto each JSON log record. Outside any request context
# (startup/shutdown logs) the key is simply absent.
REQUEST_ID_KEY = "request_id"


def get_request_id() -> str | None:
    """Return the correlation id bound to the current context, or ``None``."""
    value = get_contextvars().get(REQUEST_ID_KEY)
    return value if isinstance(value, str) else None


class RequestIDMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that assigns and propagates a per-request id.

    - Reuses a client-supplied ``X-Request-ID`` header when present, so a
      correlation id assigned upstream (e.g. by the edge) survives.
    - Otherwise generates a fresh UUID4.
    - Binds the value onto :mod:`structlog.contextvars` for the duration of
      the request and injects it into the response headers.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Bind a request id for the call and echo it in the response."""
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            incoming.strip() if incoming and incoming.strip() else str(uuid.uuid4())
        )
        bind_contextvars(**{REQUEST_ID_KEY: request_id})
        try:
            response: Response = await call_next(request)  # type: ignore[operator]
        finally:
            unbind_contextvars(REQUEST_ID_KEY)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
