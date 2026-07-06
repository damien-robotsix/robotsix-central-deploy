"""Shared structured-logging configuration.

Provides ``LOGGING_CONFIG`` — a stdlib ``dictConfig``-compatible dictionary
that bridges structlog's ``ProcessorFormatter`` into uvicorn's ``log_config``
parameter.  This keeps the uvicorn startup banner human-readable while
emitting access logs and application logs as JSON to stdout.
"""

from __future__ import annotations

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
                "structlog.stdlib.ProcessorFormatter.remove_processors_meta",
                "structlog.processors.JSONRenderer",
            ],
            "foreign_pre_chain": [
                "structlog.stdlib.add_log_level",
                "structlog.stdlib.add_logger_name",
                "structlog.processors.TimeStamper",
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
            "level": "INFO",
            "propagate": False,
        },
    },
}
