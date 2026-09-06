"""Request-ID / correlation-ID tracking middleware.

Reads an inbound ``X-Request-ID`` header (or generates a UUID4 when the
client did not supply one), stores it in a :class:`contextvars.ContextVar`
so that any code running within the request — including structlog/stdlib
log records — can correlate its output, and echoes the value back to the
client in the ``X-Request-ID`` response header.

The context variable is exposed via :func:`get_request_id` and consumed by
the structlog processor in :mod:`._logging`, which stamps every JSON log
record emitted during a request with its ``request_id``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Canonical header name for the correlation id (both directions).
REQUEST_ID_HEADER = "X-Request-ID"

# Holds the current request's correlation id. Defaults to ``None`` outside
# any request context (e.g. startup/shutdown logs), which the logging
# processor renders as a null ``request_id`` field.
_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the correlation id bound to the current context, or ``None``."""
    return _request_id_ctx.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that assigns and propagates a per-request id.

    - Reuses a client-supplied ``X-Request-ID`` header when present, so a
      correlation id assigned upstream (e.g. by the edge) survives.
    - Otherwise generates a fresh UUID4.
    - Binds the value to a context variable for the duration of the request
      and injects it into the response headers.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Bind a request id for the call and echo it in the response."""
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            incoming.strip() if incoming and incoming.strip() else str(uuid.uuid4())
        )
        token = _request_id_ctx.set(request_id)
        try:
            response: Response = await call_next(request)  # type: ignore[operator]
        finally:
            _request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
