"""Structured-logging configuration for the lifecycle server.

The structlog + stdlib ``ProcessorFormatter`` JSON bridge is no longer
hand-rolled here: it is delegated to the shared
:func:`robotsix_llmio.logging.setup_structlog` helper (which every other
structlog consumer in the fleet already uses).  ``setup_structlog`` wires a
single root ``ProcessorFormatter`` so structlog-native *and* foreign stdlib
records (e.g. ``uvicorn.access``) render through one JSON renderer, stamps
the active OpenTelemetry trace id, and — with ``correlation_id=True`` —
merges any value bound through :mod:`structlog.contextvars` onto every
event.

Two pieces stay central-deploy-specific because the helper does not provide
them:

* the per-request ``request_id`` field, bound onto ``structlog.contextvars``
  by :class:`~robotsix_central_deploy.lifecycle.request_id_middleware.RequestIDMiddleware`
  and merged onto every record via ``correlation_id=True``; and
* :data:`UVICORN_LOG_CONFIG`, a minimal uvicorn ``log_config`` that keeps the
  human-readable startup banner on stderr while letting access logs
  propagate to the shared JSON root bridge.
"""

from __future__ import annotations

from typing import TextIO

from robotsix_llmio.logging import setup_structlog


def configure_logging(
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog + stdlib logging via the shared llmio helper.

    ``correlation_id=True`` enables ``structlog.contextvars.merge_contextvars``
    so the per-request id bound by
    :class:`~robotsix_central_deploy.lifecycle.request_id_middleware.RequestIDMiddleware`
    under the ``request_id`` key is stamped onto every JSON log record — the
    same field the previous bespoke ``ProcessorFormatter`` chain emitted.

    Args:
        level: Explicit log level (name or int). Falls back to the
            ``LOG_LEVEL`` env var, then ``"INFO"``.
        stream: Target stream for the JSON handler. Defaults to
            :data:`sys.stdout`.
    """
    setup_structlog(
        fmt="json",
        level=level,
        loggers=("robotsix_central_deploy",),
        stream=stream,
        correlation_id=True,
    )


# Minimal uvicorn ``log_config``.  ``setup_structlog`` installs the JSON
# handler on the *root* logger, so this config deliberately carries no
# ``"root"`` key: ``dictConfig`` must not replace that handler.  The startup
# banner (emitted on ``uvicorn.error``) stays human-readable on stderr and
# does NOT reach the root bridge, while access logs (``uvicorn.access``)
# carry no own handler and propagate up to the root JSON bridge.
UVICORN_LOG_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
    },
}
