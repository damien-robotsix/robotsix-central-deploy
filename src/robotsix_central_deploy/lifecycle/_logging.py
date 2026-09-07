"""Shared structured-logging configuration.

Provides ``LOGGING_CONFIG`` — a stdlib ``dictConfig``-compatible dictionary
that bridges structlog's ``ProcessorFormatter`` into uvicorn's ``log_config``
parameter.  This keeps the uvicorn startup banner human-readable while
emitting access logs and application logs as JSON to stdout.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import structlog

from .request_id_middleware import get_request_id


def add_request_id(
    logger: object,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor: stamp the current correlation id onto the record.

    Reads the per-request id bound by
    :class:`~robotsix_central_deploy.lifecycle.request_id_middleware.RequestIDMiddleware`.
    Outside any request context (startup/shutdown logs) the value is
    ``None``, which renders as a null ``request_id`` field.
    """
    event_dict["request_id"] = get_request_id()
    return event_dict


# ``dictConfig``'s "()" key resolves a dotted-path string to a class to
# instantiate, but that resolution does NOT recurse into a formatter's other
# keys — ``processors``/``foreign_pre_chain`` must be actual callables here,
# not dotted-path strings. Passing strings makes ProcessorFormatter.format()
# try to call the string itself as a processor, raising "TypeError: 'str'
# object is not callable" on every single log record (silently swallowing
# the real message — see structlog/stdlib.py's ProcessorFormatter.format).
LOGGING_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
        },
        "json": {
            "()": "structlog.stdlib.ProcessorFormatter",
            "processors": [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            "foreign_pre_chain": [
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                add_request_id,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "structured": {
            "formatter": "json",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {
            "handlers": ["structured"],
            "level": "INFO",
            "propagate": False,
        },
        "robotsix_central_deploy": {
            "handlers": ["structured"],
            "level": "NOTSET",
            "propagate": False,
        },
    },
}
